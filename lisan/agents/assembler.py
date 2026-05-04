from ..tools.assembler import assemble_context
from .base import AgentResult, PromptAgent


class AssemblerAgent(PromptAgent):
    name = "assembler"
    prompt_file = "assembler_v1"

    def run(self, user_input: str, significance: str = "medium", provider: str | None = None, model: str | None = None, schema=None) -> AgentResult:
        return AgentResult(text=assemble_context(user_input, vault=self.vault))
