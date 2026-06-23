"""Plugin-provided Hermes skill registration for Claworld."""

from __future__ import annotations

from pathlib import Path


SKILL_DESCRIPTIONS = {
    "claworld-help": "Diagnose Claworld setup and support issues.",
    "claworld-main-session": "Use Claworld worlds, people, and conversations.",
    "claworld-management-session": "Handle Claworld background notifications.",
    "claworld-manage-worlds": "Create and manage Claworld worlds.",
}


def register_skills(ctx) -> None:
    """Register Claworld skills through the Hermes plugin skill API."""

    skills_root = Path(__file__).resolve().parent / "skills"
    for name, description in SKILL_DESCRIPTIONS.items():
        ctx.register_skill(name, skills_root / name / "SKILL.md", description=description)
