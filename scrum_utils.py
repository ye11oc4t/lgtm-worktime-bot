import re
from collections.abc import Iterable, Mapping
from datetime import date
from urllib.parse import urlparse


SENSITIVE_PATTERNS = (
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack 토큰", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Slack Webhook URL", re.compile(r"https://hooks\.slack\.com/services/\S+", re.I)),
    ("GitHub 토큰", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    (
        "Discord 토큰",
        re.compile(
            r"\b(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,})\b"
        ),
    ),
    ("개인키", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "비밀번호/토큰으로 보이는 값",
        re.compile(r"\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+", re.I),
    ),
)

MASS_MENTION_PATTERN = re.compile(
    r"(?:@everyone|@here|<!channel>|<!here>|<!everyone>)", re.I
)


def find_scrum_safety_issues(values: Iterable[str]) -> list[str]:
    text = "\n".join(values)
    issues: list[str] = []
    if MASS_MENTION_PATTERN.search(text):
        issues.append("전체 알림 멘션")
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            issues.append(label)
    return issues


def validate_slack_webhook_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hooks.slack.com"
        or not parsed.path.startswith("/services/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Slack Webhook URL 설정이 올바르지 않아요.")
    return value


def build_slack_payload(
    display_name: str,
    work_date: date,
    report: Mapping[str, str],
) -> dict:
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"일일 업무보고 · {work_date:%Y-%m-%d}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "plain_text",
                "text": f"작성자: {display_name}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]
    sections = (
        ("🧩 담당 모듈 / 작업 영역", report["module"]),
        ("✅ 완료한 일", report["completed"]),
        ("🔄 진행 중인 일", report["in_progress"]),
        ("➡️ 다음에 할 일", report["next_tasks"]),
        ("🚧 어려웠던 점 / 비고", report["blockers_notes"] or "없음"),
    )
    for heading, value in sections:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": f"{heading}\n{value}",
                    "emoji": True,
                },
            }
        )

    return {
        "text": "새 일일 업무보고가 등록되었습니다.",
        "blocks": blocks,
    }
