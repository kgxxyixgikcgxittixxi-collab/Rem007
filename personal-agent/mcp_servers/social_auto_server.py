"""social_auto_server — MCP wrapper cho social_auto.py (standalone 24/7 script).

social_auto.py là script độc lập nên extension loader (python -m mcp_servers.<module>)
không nạp được. Wrapper này expose các hàm chính thành MCP tools.
"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema
import social_auto as _sa


def social_cycle(a=None):
    return _sa.run_social_cycle()


def social_report(a=None):
    return _sa.get_daily_report()


def social_status(a=None):
    return _sa.get_platform_status()


def social_post(platform, topic, a=None):
    return _sa.generate_post_content(platform, topic)


TOOLS = [
    Tool("social_cycle", "CHẠY 1 vòng social automation 24/7 (post/reply/monitor theo cấu hình).",
         schema({}), social_cycle),
    Tool("social_report", "BÁO CÁO social automation trong ngày.",
         schema({}), social_report),
    Tool("social_status", "TRẠNG THÁI các platform (posts/interactions hôm nay).",
         schema({}), social_status),
    Tool("social_post", "TẠO nội dung post cho 1 platform từ topic.",
         schema({"platform": {"type": "string"}, "topic": {"type": "string"}}),
         social_post),
]

if __name__ == "__main__":
    Server(TOOLS, "social_auto", "0.1.0").serve(sys.stdin, sys.stdout)
