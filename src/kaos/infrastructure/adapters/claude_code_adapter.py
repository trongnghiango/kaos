"""ClaudeCodeAdapter stub implementing LLMProviderPort."""

from kaos.application.ports import LLMProviderPort
from kaos.domain.value_objects import AgentInstruction


class ClaudeCodeAdapter(LLMProviderPort):
    def get_provider_name(self) -> str:
        return "claude-code"

    async def run_agent(self, instruction: AgentInstruction) -> tuple[int, str]:
        raise NotImplementedError("ClaudeCodeAdapter is not implemented yet.")
