import os
import time
import json
import logging
import threading
import warnings
import httpx
import ssl
import certifi
from abc import ABC, abstractmethod
from typing import List, Any, Optional
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from app.core.config import settings

warnings.filterwarnings("ignore", message=".*automatic function calling.*")
warnings.filterwarnings("ignore", category=UserWarning, module="google.genai")

logger = logging.getLogger("AMALDA_API")

# AUDIT FIX 3: Global HTTPX Client for Connection Pooling
# Prevents socket leaks and connection exhaustion during heavy parallel workloads.
ssl_context = ssl.create_default_context(cafile=certifi.where())
GLOBAL_HTTPX_CLIENT = httpx.Client(
    http1=True, 
    http2=False, 
    verify=ssl_context, 
    timeout=httpx.Timeout(45.0, connect=15.0), # Added explicit connect timeout
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100) # Enforced bounds
)

class ExecutionEngine(ABC):
    def __init__(self, provider_name: str, engine_id: str, api_key: str):
        self.provider_name = provider_name
        self.engine_id = engine_id
        self.api_key = api_key
        self.model = ""
        self.status = "PENDING"
        self.cooldown_until = 0.0
        self.active_requests = 0
        self.total_success = 0
        self.total_errors = 0
        self.lock = threading.Lock()

    @property
    def is_healthy(self):
        return self.status == "VERIFIED"

    @abstractmethod
    def discover_and_verify(self): pass

    @abstractmethod
    def get_langchain_client(self, schema=None): pass


class GroqEngine(ExecutionEngine):
    def discover_and_verify(self):
        if not self.api_key:
            self.status = "SKIPPED"
            return
        self.model = "openai/gpt-oss-120b"
        self.status = "VERIFIED"
        logger.info(f"[Registry] {self.engine_id} registered. Model: {self.model} -> {self.status}")

    def get_langchain_client(self, schema=None):
        llm = ChatGroq(
            model=self.model,
            api_key=self.api_key,
            temperature=0.0,
            max_retries=0,
            http_client=GLOBAL_HTTPX_CLIENT
        )
        return llm.with_structured_output(schema, method="json_mode", include_raw=False) if schema else llm


class GeminiEngine(ExecutionEngine):
    def discover_and_verify(self):
        if not self.api_key:
            self.status = "SKIPPED"
            return
        self.model = "gemini-3.5-flash-lite"
        self.status = "VERIFIED"
        logger.info(f"[Registry] {self.engine_id} registered. Model: {self.model} -> {self.status}")

    def get_langchain_client(self, schema=None):
        llm = ChatGoogleGenerativeAI(
            model=self.model,
            google_api_key=self.api_key,
            temperature=0.0,
            max_retries=0
        )
        return llm.with_structured_output(schema) if schema else llm


class OpenRouterEngine(ExecutionEngine):
    def discover_and_verify(self):
        if not self.api_key:
            self.status = "SKIPPED"
            return
        self.model = "openrouter/free"
        self.status = "VERIFIED"
        logger.info(f"[Registry] {self.engine_id} registered. Model: {self.model} -> {self.status}")

    def get_langchain_client(self, schema=None):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://amalda.ai",
            "X-Title": "AMALDA"
        }
        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            model=self.model,
            api_key=self.api_key,
            temperature=0.0,
            max_retries=0,
            default_headers=headers,
            http_client=GLOBAL_HTTPX_CLIENT
        )
        return llm.with_structured_output(schema, method="json_mode", include_raw=False) if schema else llm


class CloudflareEngine(ExecutionEngine):
    def discover_and_verify(self):
        self.account_id = getattr(settings, "CF_ACCOUNT_ID", None)
        if not self.api_key or not self.account_id:
            self.status = "SKIPPED"
            return
        self.model = "@cf/meta/llama-3.2-3b-instruct"
        self.status = "VERIFIED"
        logger.info(f"[Registry] {self.engine_id} registered. Model: {self.model} -> {self.status}")

    def get_langchain_client(self, schema=None):
        base_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/v1"
        llm = ChatOpenAI(
            base_url=base_url,
            model=self.model,
            api_key=self.api_key,
            temperature=0.0,
            max_retries=0,
            default_headers={"Authorization": f"Bearer {self.api_key}"},
            http_client=GLOBAL_HTTPX_CLIENT
        )
        if schema:
            from langchain_core.output_parsers import PydanticOutputParser
            parser = PydanticOutputParser(pydantic_object=schema)
            return llm | parser
        return llm


class EnterpriseLLMGateway:
    def __init__(self):
        self.engines: List[ExecutionEngine] = []
        self.scheduler_lock = threading.Lock()
        self.rr_index = 0
        self._initialize_engines()

    def _initialize_engines(self):
        if getattr(settings, "GROQ_API_KEY", None):
            self.engines.append(GroqEngine("Groq", "Groq-Primary", settings.GROQ_API_KEY))
        if getattr(settings, "GROQ_API_KEY_2", None):
            self.engines.append(GroqEngine("Groq", "Groq-Secondary", settings.GROQ_API_KEY_2))
        if getattr(settings, "GEMINI_API_KEY", None):
            self.engines.append(GeminiEngine("Gemini", "Gemini-Main", settings.GEMINI_API_KEY))
        if getattr(settings, "OPENROUTER_API_KEY", None):
            self.engines.append(OpenRouterEngine("OpenRouter", "OpenRouter-Main", settings.OPENROUTER_API_KEY))
        if getattr(settings, "CLOUDFLARE_API_KEY", None):
            self.engines.append(CloudflareEngine("Cloudflare", "Cloudflare-Main", settings.CLOUDFLARE_API_KEY))

        for e in self.engines:
            e.discover_and_verify()
        healthy = [e for e in self.engines if e.is_healthy]
        logger.info(f"Production Gateway active. {len(healthy)} engine(s) VERIFIED and READY.")

    def _acquire_optimal_engine(self) -> Optional[ExecutionEngine]:
        with self.scheduler_lock:
            now = time.time()
            candidates = [e for e in self.engines if e.is_healthy and now >= e.cooldown_until]
            if not candidates:
                return None
            
            min_load = min(e.active_requests for e in candidates)
            least_loaded = [e for e in candidates if e.active_requests == min_load]
            
            chosen = least_loaded[self.rr_index % len(least_loaded)]
            self.rr_index = (self.rr_index + 1) % 1000000
            
            with chosen.lock:
                chosen.active_requests += 1
            return chosen

    def _release_engine(self, engine: ExecutionEngine, success: bool):
        with engine.lock:
            engine.active_requests = max(0, engine.active_requests - 1)
            if success:
                engine.total_success += 1
            else:
                engine.total_errors += 1

    def _normalize_error(self, e: Exception) -> tuple[str, int]:
        err_str = str(e).lower()
        status_code = getattr(e, "status_code", 500)
        
        if "401" in err_str or "unauthorized" in err_str:
            return ("AUTH_ERROR", 401)
        if "403" in err_str:
            return ("PERMISSION_ERROR", 403)
        if "402" in err_str or "balance" in err_str:
            return ("BILLING_OR_QUOTA_ERROR", 402)
        if "404" in err_str or "not found" in err_str:
            return ("ENDPOINT_OR_MODEL_ERROR", 404)
        if "429" in err_str or "rate limit" in err_str or "quota" in err_str:
            return ("RATE_LIMIT", 429)
        if "503" in err_str or "high demand" in err_str:
            return ("CAPACITY_UNAVAILABLE", 503)
        if "validation" in err_str or "parse" in err_str or "json" in err_str:
            return ("SCHEMA_VALIDATION_ERROR", 400)
        if "ssl" in err_str or "handshake" in err_str or "network" in err_str or "timed out" in err_str:
            return ("NETWORK_ERROR", 503)
        return ("PROVIDER_SERVER_ERROR", status_code)

    def invoke_with_failover(self, prompt, schema=None, estimated_tokens=500) -> Any:
        # Format injection logic preserved exactly as validated
        if schema:
            try:
                schema_dict = schema.model_json_schema()
                instruction = (
                    f"\n\nCRITICAL SYSTEM INSTRUCTION:\n"
                    f"You MUST return your answer EXCLUSIVELY as a raw JSON object.\n"
                    f"The JSON object MUST EXACTLY MATCH the following schema:\n"
                    f"{json.dumps(schema_dict, indent=2)}\n"
                    f"Do not include markdown blocks or any other text."
                )
                if hasattr(prompt, "messages") and len(prompt.messages) > 0:
                    prompt.messages[0].content += instruction
            except Exception as ex:
                logger.error(f"Failed to inject schema into prompt: {ex}")

        start_time = time.time()
        timeout_threshold = 240.0
        
        # AUDIT FIX 2: Bounded Retries
        # Prevents CPU spin-locking and localized retry storms.
        max_attempts = 15
        attempts = 0
        schema_failures_this_run = 0

        while attempts < max_attempts:
            attempts += 1
            engine = self._acquire_optimal_engine()
            
            if not engine:
                if time.time() - start_time > timeout_threshold:
                    raise TimeoutError(f"[Gateway Fatal] 240s Timeout after {attempts} attempts. All execution engines currently exhausted/rate-limited.")
                
                now = time.time()
                cooldowns = [e.cooldown_until - now for e in self.engines if e.cooldown_until > now]
                wait_time = max(1.0, min(min(cooldowns) if cooldowns else 3.0, 10.0))
                logger.warning(f"[Scheduler] Concurrency/Rate Limits saturated. Yielding for {wait_time:.1f}s...")
                threading.Event().wait(wait_time)
                continue

            try:
                llm = engine.get_langchain_client(schema)
                result = llm.invoke(prompt)
                self._release_engine(engine, success=True)
                return result

            except Exception as e:
                self._release_engine(engine, success=False)
                err_category, status_code = self._normalize_error(e)
                
                with engine.lock:
                    if status_code in (401, 402, 403):
                        engine.status = "AUTH_FAILED"
                        engine.cooldown_until = time.time() + 3600
                        logger.error(f"[Engine Fatal] {engine.engine_id} disabled due to {err_category}.")
                    
                    elif status_code in (429, 503):
                        backoff = 10.0
                        if hasattr(e, "response") and "retry-after" in getattr(e.response, "headers", {}):
                            try:
                                backoff = float(e.response.headers["retry-after"])
                            except Exception:
                                pass
                        engine.cooldown_until = time.time() + backoff
                        logger.warning(f"[Engine Throttled] {engine.engine_id} hit {err_category}. Cooldown: {backoff:.1f}s.")
                        
                    elif status_code == 404:
                        engine.status = "MODEL_ERROR"
                        engine.cooldown_until = time.time() + 3600
                        logger.error(f"[Engine Fatal] {engine.engine_id} disabled. Model unmapped ({err_category}).")
                        
                    elif status_code == 400:
                        schema_failures_this_run += 1
                        if schema_failures_this_run > 3:
                            logger.error(f"[Gateway Escalation] Schema validation failed {schema_failures_this_run} times. Escalating.")
                            raise e 
                        engine.cooldown_until = time.time() + 2.0
                        logger.error(f"[Engine Schema Error] {engine.engine_id} failed JSON validation. Attempt {schema_failures_this_run}/3")
                    
                    else:
                        engine.cooldown_until = time.time() + min(30, (2 ** min(engine.total_errors, 5)))
                        logger.error(f"[Engine Network] {engine.engine_id} hit {err_category}. Retrying later.")
        
        raise RuntimeError(f"[Gateway Fatal] Maximum attempts ({max_attempts}) exceeded. Aborting WorkUnit to prevent retry storm.")

llm_gateway = EnterpriseLLMGateway()