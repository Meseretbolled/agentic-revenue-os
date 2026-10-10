"""Signals that inbound text is trying to instruct the model.

This is a FLAGGING signal, not the security boundary. It is a heuristic and an attacker can get
around it (new phrasing, a language not listed, encodings). What actually protects the system is
that flagged text is quarantined for a person, that no model output can send or approve anything
by itself (the policy engine and the send gate decide), and that tools are scoped. Any single
rule hit is enough to flag; there is no score threshold that lets a weak hit through.
"""

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠⁡⁢⁣﻿­"))


@dataclass(frozen=True)
class Finding:
    rule_id: str
    snippet: str            # what matched (the attacker's own words), trimmed
    hidden: bool = False    # found in text a human reader would not see


def _rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags | re.IGNORECASE)


_AI = (r"(?:ai|a\.i\.|llms?|language models?|assistants?|agents?|bots?|chat ?bots?|gpt|claude|"
       r"models?|crawlers?)")
_DET = r"(?:all\s+|any\s+|your\s+|the\s+|my\s+|these\s+|those\s+|every\s+)*"

# (rule id, pattern). Patterns run on normalised text: NFKC, zero-width characters removed,
# case-folded, whitespace collapsed.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "ignore previous instructions", per language
    ("override.en", _rx(rf"\b(?:ignore|disregard|forget|override|bypass|skip)\s+{_DET}"
                       r"(?:(?:previous|prior|above|earlier|preceding|former|system|original)\s+)?"
                       r"(?:instructions?|rules?|guidelines?|prompts?|directives?|polic(?:y|ies)|"
                       r"constraints|restrictions)\b")),
    ("override.es", _rx(r"\b(?:ignora|ignore|olvida|omite)\s+(?:todas\s+)?(?:las\s+|tus\s+|mis\s+)?"
                        r"(?:instrucciones|reglas|indicaciones)")),
    ("override.fr", _rx(r"\b(?:ignore|ignorez|oublie|oubliez)\s+(?:toutes\s+)?(?:les\s+|tes\s+|vos\s+)?"
                        r"(?:instructions|consignes|r[eè]gles)")),
    ("override.de", _rx(r"\b(?:ignoriere|ignorieren sie|vergiss|vergessen sie|missachte)\s+(?:alle\s+)?"
                        r"(?:(?:vorherigen|bisherigen|obigen|vorigen|deine|ihre)\s+)*"
                        r"(?:anweisungen|instruktionen|regeln|vorgaben)")),
    ("override.pt", _rx(r"\b(?:ignore|ignora|esque[cç]a)\s+(?:todas\s+)?(?:as\s+)?(?:suas\s+)?"
                        r"(?:instru[cç][oõ]es|regras)")),
    ("override.it", _rx(r"\b(?:ignora|ignorate|dimentica)\s+(?:tutte\s+)?(?:le\s+)?(?:tue\s+)?"
                        r"(?:istruzioni|regole)")),
    ("override.ru", _rx(r"(?:игнорируй|игнорируйте|забудь|забудьте)\s+(?:все\s+)?"
                        r"(?:(?:предыдущие|прошлые|прежние|свои)\s+)?(?:инструкции|указания|правила)")),
    ("override.zh", _rx(r"(?:忽略|无视|無視|忘记|忘記)(?:之前|以前|先前|上面|以上|所有|全部|的|你的)*"
                        r"(?:指令|指示|说明|說明|提示|规则|規則|命令)")),
    ("override.ja", _rx(r"(?:指示|命令|ルール|指令|プロンプト)を?(?:すべて)?(?:無視|忘れ)")),
    ("override.ar", _rx(r"(?:تجاهل|انس)\s+(?:كل\s+|جميع\s+)?(?:ال)?(?:تعليمات|أوامر|اوامر)")),
    # addressing the model directly
    ("address.ai", _rx(rf"\b(?:note|message|instructions?|attention|notice|reminder|directive)s?\s+"
                       rf"(?:to|for)\s+(?:the\s+|all\s+|any\s+)?(?:automated\s+)?{_AI}\b")),
    ("address.ai_prefix", _rx(rf"(?:^|[.!?\n]\s*)(?:dear\s+)?{_AI}(?:\s+\w+){{0,2}}\s*[:,]")),
    ("address.ai_label", _rx(r"\b(?:ai|llm|assistant|agent|bot|system)\s+"
                             r"(?:instructions?|directive|command|override|message)\s*:")),
    ("role.reassign", _rx(r"\b(?:you are now|from now on,? you|pretend (?:to be|you are|that you)|"
                          r"new instructions?|act as (?:an? )?(?:ai|assistant|agent|bot|llm|different|"
                          r"unrestricted)|developer mode|jailbreak|do anything now)\b")),
    ("markup.chat", _rx(r"<\|(?:im_start|im_end|system|assistant|user)\|>|\[/?(?:inst|system)\]|"
                        r"<</?sys>>|</?system>|###\s*(?:system|instructions?)\b|^\s*(?:system|assistant)\s*:",
                        re.MULTILINE)),
    ("leak.prompt", _rx(r"\b(?:reveal|show|print|repeat|output|leak|display)\s+(?:me\s+)?(?:your|the)\s+"
                        r"(?:hidden\s+|secret\s+|full\s+)?(?:system\s+|initial\s+)?(?:prompt|instructions)\b")),
    # steering the system's own actions
    ("steer.mark", _rx(r"\bmark\s+(?:this|the|our)\s+(?:lead|deal|account|company|prospect)\s+as\b")),
    ("steer.conceal", _rx(r"\b(?:do not|don't|never)\s+(?:tell|inform|alert|notify)\s+(?:the\s+)?"
                          r"(?:user|human|reviewer|operator|sales ?rep|owner)\b")),
    ("steer.bypass_review", _rx(r"\b(?:without|skip|bypass)\s+(?:any\s+)?(?:human\s+)?"
                                r"(?:review|approval|oversight)\b")),
    ("boundary.forgery", _rx(r"<<\s*/?\s*untrusted|</?\s*untrusted\b")),
)

# Phrases that survive being spelled out with spaces or punctuation ("i.g.n.o.r.e ...").
_SQUASHED = re.compile(r"(?:ignore|disregard|forget|override)(?:all|any|your|the)*(?:previous|prior|above|"
                       r"earlier)?(?:instructions|rules|prompt)|systemprompt")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return " ".join(text.casefold().split())


def scan(text: str, *, hidden: bool = False) -> list[Finding]:
    """Every rule that matches. An empty list means no signal, not that the text is safe."""
    norm = normalise(text)
    findings = [Finding(rule_id, m.group(0)[:160], hidden)
                for rule_id, rx in RULES if (m := rx.search(norm))]
    if not findings:
        squashed = re.sub(r"[\W_]+", "", norm)
        if m := _SQUASHED.search(squashed):
            findings.append(Finding("override.obfuscated", m.group(0)[:160], hidden))
    return findings


# --- HTML: separate what a person sees from what a scraper would still read ------------------

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
         "track", "wbr"}
_HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|"
                           r"font-size\s*:\s*0(?:px|pt|em|%)?\s*(?:;|$)|"
                           r"opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|text-indent\s*:\s*-\d{3,}|"
                           r"(?:left|top)\s*:\s*-\d{4,}|height\s*:\s*0(?:px)?\s*;[^\"]*overflow\s*:\s*hidden",
                           re.IGNORECASE)
_TEXT_ATTRS = ("alt", "title", "aria-label", "placeholder", "content")
_BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "section", "article", "header", "footer",
           "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible: list[str] = []
        self.hidden: list[str] = []
        self._stack: list[tuple[str, bool]] = []

    def _is_hidden(self) -> bool:
        return any(flag for _, flag in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        for name in _TEXT_ATTRS:
            if a.get(name, "").strip():
                self.hidden.append(a[name])
        hides = (
            "hidden" in a or a.get("aria-hidden", "").lower() == "true"
            or bool(_HIDING_STYLE.search(a.get("style", ""))) or tag in {"script", "style", "template"}
        )
        if tag in _BLOCKS and not self._is_hidden() and not hides:
            self.visible.append("\n")
        if tag not in _VOID:
            self._stack.append((tag, hides))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS and not self._is_hidden():
            self.visible.append("\n")
        a = {k.lower(): (v or "") for k, v in attrs}
        for name in _TEXT_ATTRS:
            if a.get(name, "").strip():
                self.hidden.append(a[name])

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCKS and not self._is_hidden():
            self.visible.append("\n")
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            (self.hidden if self._is_hidden() else self.visible).append(data)
        elif not self._is_hidden() and data:
            self.visible.append(" ")

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.hidden.append(data)


def extract_html(html: str) -> tuple[str, tuple[str, ...]]:
    """(visible text, hidden text pieces). Hidden text never goes to a model."""
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.visible).split("\n"))
    return "\n".join(line for line in lines if line), tuple(h.strip() for h in parser.hidden)
