"""Mock adapters for every port, with the same interfaces as the real ones will have."""

from dataclasses import dataclass

from revos.adapters.mock.calendar import MockCalendar
from revos.adapters.mock.chat import MockChat
from revos.adapters.mock.crm import MockCRM
from revos.adapters.mock.esign import MockESign
from revos.adapters.mock.mailer import MockInbox, MockMailer
from revos.adapters.mock.research import MockResearch
from revos.adapters.mock.transcripts import MockTranscripts
from revos.adapters.router import ActionRouter


@dataclass
class MockWorld:
    mailer: MockMailer
    inbox: MockInbox
    crm: MockCRM
    chat: MockChat
    transcripts: MockTranscripts
    esign: MockESign
    research: MockResearch
    calendar: MockCalendar

    def routes(self) -> dict:
        return {"first_touch": self.mailer, "follow_up": self.mailer, "crm_update": self.crm,
                "crm_note": self.crm, "send_io": self.esign, "book_meeting": self.calendar,
                "handoff": self.chat}

    @property
    def router(self) -> ActionRouter:
        """The outbox provider: mail, CRM writes and e-signature, each to its own mock."""
        return ActionRouter(self.routes())


def build_mock_world() -> MockWorld:
    return MockWorld(MockMailer(), MockInbox(), MockCRM(), MockChat(), MockTranscripts(),
                     MockESign(), MockResearch(), MockCalendar())


__all__ = ["MockWorld", "build_mock_world", "MockMailer", "MockInbox", "MockCRM", "MockChat",
           "MockTranscripts", "MockESign", "MockResearch", "MockCalendar"]
