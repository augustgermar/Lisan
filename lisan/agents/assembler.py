from ..tools.assembler import assemble_context
from .base import AgentResult, PromptAgent


class AssemblerAgent(PromptAgent):
    name = "assembler"
    prompt_file = "assembler_v1"

    def run(
        self,
        user_input: str,
        significance: str = "medium",
        provider: str | None = None,
        model: str | None = None,
        schema=None,
        conversation_id: str | None = None,
        domain: str | None = None,
        arena: str | None = None,
    ) -> AgentResult:
        # Finding #12: forward conversation_id so the cross-conversation
        # "Recent Activity" preamble fires on the extraction path. The
        # elicitor path already passes it through elicitor_session.
        # Optional follow-up: also thread domain / arena so the extraction
        # path can override the heuristic domain inference downstream.
        return AgentResult(text=assemble_context(
            user_input,
            vault=self.vault,
            conversation_id=conversation_id,
            domain=domain,
            arena=arena,
        ))
