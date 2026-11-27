#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
#
# Modified copy of the OpenTelemetry Demo 3.1.0 agent's LLM client (src/agents/llm.py). Mounted over
# /app/src/agents/llm.py in the agent container (see compose.bedrock.yaml). One addition:
# weighted per-conversation model selection via LLM_MODEL_WEIGHTS, e.g.
#     LLM_MODEL_WEIGHTS=claude-haiku-5-5:70,claude-sonnet-5-5:30
# When the variable is unset or malformed, behaviour is identical to upstream (LLM_MODEL for every call).
# ChatLLM() is constructed once per conversation (agents.run_agent), so every LLM call of one conversation
# uses the same model, and gen_ai.request.model / gen_ai.response.model on the spans name the real model.

import asyncio
import logging
import os
import random
import threading
import time

import httpx
from langchain_openai import ChatOpenAI
from src.agents.feature_flags import get_int_feature_flag
from src.agents.patch_vcr import VCR


# vcrpy patches global state, so overlapping cassette contexts leave stale patches behind
_async_vcr_lock = asyncio.Lock()
_sync_vcr_lock = threading.Lock()


def _parse_model_weights(raw):
    """'a:70,b:30' -> ([a, b], [70.0, 30.0]); None when unusable."""
    names, weights = [], []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, weight = part.rpartition(":")
        if not name:  # no colon: bare model name, weight 1
            name, weight = weight, "1"
        try:
            w = float(weight)
        except ValueError:
            logging.warning("LLM_MODEL_WEIGHTS: bad weight in %r, ignoring the whole setting", part)
            return None
        if w > 0:
            names.append(name.strip())
            weights.append(w)
    return (names, weights) if names else None


def pick_model(default):
    parsed = _parse_model_weights(os.getenv("LLM_MODEL_WEIGHTS"))
    if not parsed:
        return default
    names, weights = parsed
    return random.choices(names, weights=weights, k=1)[0]


class ChatLLM(ChatOpenAI):
    def __init__(self, **kwargs):
        model_name = pick_model(os.getenv("LLM_MODEL", "default"))
        use_vcr = os.getenv("USE_VCR", "True").lower() == "true"
        llm_tls_verify = os.getenv("LLM_TLS_VERIFY", "True").lower() == "true"
        cassette_name = ""
        if use_vcr:
            cassette_name = f"{model_name.replace('/', '_')}_cassette.yaml"
        if "http_async_client" not in kwargs:
            kwargs["http_async_client"] = httpx.AsyncClient(verify=llm_tls_verify)
        kwargs.setdefault("openai_api_base", os.getenv("LLM_BASE_URL"))
        kwargs.setdefault("model", model_name)

        api_key = os.getenv("API_KEY")
        if not api_key:
            api_key = "sk-dummy"
        kwargs.setdefault("api_key", api_key)

        super().__init__(**kwargs)

        object.__setattr__(self, "_use_vcr", use_vcr)
        object.__setattr__(self, "_cassette_name", cassette_name)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        delay_ms = get_int_feature_flag("aiSlowResponse")
        if delay_ms:
            time.sleep(delay_ms / 1000)
        if getattr(self, "_use_vcr", False):
            with _sync_vcr_lock, VCR.use_cassette(self._cassette_name):
                return super()._generate(messages, stop, run_manager, **kwargs)
        return super()._generate(messages, stop, run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        delay_ms = get_int_feature_flag("aiSlowResponse")
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        if getattr(self, "_use_vcr", False):
            async with _async_vcr_lock:
                with VCR.use_cassette(self._cassette_name):
                    return await super()._agenerate(messages, stop, run_manager, **kwargs)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)
