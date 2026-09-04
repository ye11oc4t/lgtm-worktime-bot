import unittest
from datetime import date

from scrum_utils import (
    build_slack_payload,
    find_scrum_safety_issues,
    validate_slack_webhook_url,
)


class ScrumSafetyTest(unittest.TestCase):
    def test_safe_report_has_no_issues(self) -> None:
        self.assertEqual(
            find_scrum_safety_issues(
                [
                    "Attack Rule Engine",
                    "정책 파서 구현 완료",
                    "그래프 연결 규칙 구현 중",
                    "테스트 케이스 추가 예정",
                    "API 응답 형식 확인 필요",
                ]
            ),
            [],
        )

    def test_detects_mass_mentions_and_secrets(self) -> None:
        issues = find_scrum_safety_issues(
            [
                "@everyone 확인",
                "token=super-secret-value",
                "AKIAABCDEFGHIJKLMNOP",
                "https://hooks.slack.com/services/T000/B000/SECRET",
                "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
            ]
        )
        self.assertIn("전체 알림 멘션", issues)
        self.assertIn("비밀번호/토큰으로 보이는 값", issues)
        self.assertIn("AWS Access Key", issues)
        self.assertIn("Slack Webhook URL", issues)
        self.assertIn("GitHub 토큰", issues)

    def test_slack_payload_uses_plain_text_for_user_content(self) -> None:
        payload = build_slack_payload(
            "<!channel>",
            date(2026, 9, 4),
            {
                "module": "Scanner",
                "completed": "<@U123> 작업 완료",
                "in_progress": "연동 중",
                "next_tasks": "검증 예정",
                "blockers_notes": "없음",
            },
        )
        self.assertEqual(payload["text"], "새 일일 업무보고가 등록되었습니다.")
        for block in payload["blocks"]:
            if "text" in block:
                self.assertEqual(block["text"]["type"], "plain_text")

    def test_slack_webhook_url_is_restricted(self) -> None:
        valid = "https://hooks.slack.com/services/T000/B000/SECRET"
        self.assertEqual(validate_slack_webhook_url(valid), valid)
        for invalid in (
            "http://hooks.slack.com/services/T000/B000/SECRET",
            "https://evil.example/services/T000/B000/SECRET",
            "https://hooks.slack.com.evil.example/services/T000/B000/SECRET",
            "https://user@hooks.slack.com/services/T000/B000/SECRET",
            "https://hooks.slack.com/services/T000/B000/SECRET?copy=true",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_slack_webhook_url(invalid)


if __name__ == "__main__":
    unittest.main()
