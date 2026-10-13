"""Environment-backed runtime configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    called_number: Optional[str]
    agent_name: str
    stt_model: str
    llm_model: str
    grader_model: str
    tts_model: str
    tts_voice: str
    portal_url: str
    portal_access_code: Optional[str]
    portal_tester_name: Optional[str]
    portal_tester_email: Optional[str]
    portal_persona_id: Optional[str]

    @classmethod
    def from_env(cls, require_supabase: bool = True) -> "Settings":
        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if require_supabase and (not supabase_url or not service_key):
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env.local"
            )

        called_number = normalize_phone(os.getenv("QA_CALLED_NUMBER", "")) or None
        return cls(
            supabase_url=supabase_url,
            supabase_service_role_key=service_key,
            called_number=called_number,
            agent_name=os.getenv("QA_AGENT_NAME", "qa-simulated-caller").strip(),
            stt_model=os.getenv(
                "QA_STT_MODEL", "assemblyai/universal-3-5-pro"
            ).strip(),
            llm_model=os.getenv("QA_LLM_MODEL", "google/gemma-4-31b-it").strip(),
            grader_model=os.getenv("QA_GRADER_MODEL", "openai/gpt-4.1-mini").strip(),
            tts_model=os.getenv("QA_TTS_MODEL", "fishaudio/s2.1-pro").strip(),
            tts_voice=os.getenv(
                "QA_TTS_VOICE", "fa4c9eb3dccc4806b382b40d61c6b10a"
            ).strip(),
            portal_url=os.getenv(
                "WEDGE_PORTAL_URL", "https://test-portal-livid.vercel.app"
            ).strip().rstrip("/"),
            portal_access_code=os.getenv("WEDGE_PORTAL_ACCESS_CODE", "").strip() or None,
            portal_tester_name=os.getenv("WEDGE_PORTAL_TESTER_NAME", "").strip() or None,
            portal_tester_email=os.getenv("WEDGE_PORTAL_TESTER_EMAIL", "").strip() or None,
            portal_persona_id=os.getenv("WEDGE_PORTAL_PERSONA_ID", "").strip() or None,
        )


def normalize_phone(value: str) -> str:
    """Normalize a US/international phone string to a conservative E.164-like value."""

    value = value.strip()
    if not value:
        return ""
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        return ""
    if value.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def is_e164(value: str) -> bool:
    return re.fullmatch(r"\+[1-9][0-9]{7,14}", value) is not None


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
