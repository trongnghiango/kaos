"""
Extract Schema Use Case
=======================
Trích xuất database schema từ TypeScript codebase qua Gatekeeper.
"""

import logging

from kaos.application.ports import GatekeeperPort

logger = logging.getLogger("STAX_Harness")


class ExtractSchemaUseCase:
    """Use case trích xuất database schema từ TypeScript codebase"""

    def __init__(self, gatekeeper: GatekeeperPort):
        self.gatekeeper = gatekeeper

    async def execute(self) -> dict:
        logger.info("🔍 [KAOS] Đang trích xuất database schema qua Gatekeeper...")
        return await self.gatekeeper.extract_schema()
