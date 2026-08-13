from enum import Enum

from .static import prompt_templates, system_directives


class TemplateBucket(Enum):
    """Which static module a template_id lives in — not the deleted
    sync-pipeline BucketEnum (claude.md §3.5), a much smaller closed set
    scoped to this parser alone: Bucket B (verbatim scripts, returned to
    the patient unmodified) vs. Bucket C (directives fed into the LLM's
    system prompt to guide drafting, never recited verbatim)."""

    B = "prompt_templates"
    C = "system_directives"


_BUCKET_MODULES = {
    TemplateBucket.B: prompt_templates,
    TemplateBucket.C: system_directives,
}


class TemplateNotFoundError(Exception):
    """Raised when (bucket, template_id) doesn't resolve to a real
    module-level Template variable — never silently returns an empty
    string or a placeholder, since Bucket B content in particular is
    returned to the patient verbatim and a silent miss would ship a
    literal `$placeholder`-shaped bug straight into a WhatsApp reply."""


class TemplateParser:
    """Resolves (bucket, template_id) -> rendered string (claude.md §1.1's
    documented contract, built out for Section 3 Step 1). Stateless — the
    two static modules it reads from are hand-authored, hardcoded Python
    (claude.md §1.3's one deliberate exception), never regenerated or
    per-client, so there is nothing to inject or configure here."""

    def resolve(self, bucket: TemplateBucket, template_id: str, **substitutions) -> str:
        module = _BUCKET_MODULES[bucket]
        template = getattr(module, template_id, None)

        if template is None:
            raise TemplateNotFoundError(
                f"No template_id={template_id!r} in bucket={bucket.name!r} "
                f"({module.__name__})"
            )

        # .substitute (not .safe_substitute): a missing placeholder value
        # must fail loudly here, never ship a literal "$placeholder" into
        # a patient-facing reply.
        return template.substitute(**substitutions)
