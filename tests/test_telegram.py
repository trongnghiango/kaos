#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path

# Add src to pythonpath
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kaos.infrastructure.adapters.telegram_adapter import TelegramAdapter

async def test_telegram_polling():
    print("🧪 [Test Telegram] Khởi chạy kiểm thử Telegram Adapter...")
    
    # Khởi tạo adapter với mock tokens
    bot = TelegramAdapter(token="123456:mock_token", chat_id="987654321", polling_interval=0.1)
    
    # Mock handler cho command /status
    status_called = False
    async def mock_status_handler(chat_id, args):
        nonlocal status_called
        status_called = True
        print(f"   📥 Command /status received with args: '{args}'")

    git_status_called = False
    async def mock_git_status_handler(chat_id, args):
        nonlocal git_status_called
        git_status_called = True
        print(f"   📥 Command /git_status received")

    bot.register_command("status", mock_status_handler)
    bot.register_command("git_status", mock_git_status_handler)
    
    # Giả lập xử lý getUpdates json trả về lệnh /status & /git_status
    mock_update_1 = {
        "update_id": 10001,
        "message": {
            "message_id": 45,
            "from": {"id": 987654321, "first_name": "TestUser"},
            "chat": {"id": 987654321, "type": "private"},
            "date": 1624888800,
            "text": "/status detailed_report"
        }
    }
    mock_update_2 = {
        "update_id": 10002,
        "message": {
            "message_id": 46,
            "from": {"id": 987654321, "first_name": "TestUser"},
            "chat": {"id": 987654321, "type": "private"},
            "date": 1624888801,
            "text": "/git_status"
        }
    }
    
    # Mock aiohttp session để không tạo kết nối thật ra ngoài internet
    class MockResponse:
        def __init__(self, status, data):
            self.status = status
            self._data = data
        async def json(self):
            return self._data
        async def text(self):
            return str(self._data)
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockSession:
        def post(self, url, json=None, **kwargs):
            print(f"   📤 Mock POST request to: {url} | Payload: {json}")
            return MockResponse(200, {"ok": True})
        def get(self, url, params=None, **kwargs):
            # Trả về update giả lập cho lần poll đầu tiên, sau đó trả về rỗng để dừng
            if params and params.get("offset", 0) == 0:
                return MockResponse(200, {"ok": True, "result": [mock_update_1, mock_update_2]})
            return MockResponse(200, {"ok": True, "result": []})
        async def close(self):
            pass

    bot._session = MockSession()
    
    # Chạy poll một lần để xử lý update giả lập
    print("   🔄 Giả lập nhận message /status và /git_status từ User...")
    await bot._poll_once()
    
    # Kiểm tra xem handler đã được gọi chưa
    assert status_called is True, "Handler cho status chưa được gọi!"
    assert git_status_called is True, "Handler cho git_status chưa được gọi!"
    assert bot._offset == 10003, "Telegram offset chưa được tăng chính xác!"
    print("🎉 Test Telegram Adapter PASSED!")

if __name__ == "__main__":
    asyncio.run(test_telegram_polling())
