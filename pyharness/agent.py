from __future__ import annotations

import re

from .executor import Executor
from .llm import LLMProvider, Message, Tier
from .permissions import RulePolicy
from .prompts import SYSTEM_PROMPT
from .session import Session
from .tools import Toolbox

_CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)


def extract_code(reply: str) -> str | None:
    """Return the first Python code block in `reply`, or None if there is none."""
    m = _CODE_BLOCK.search(reply)
    return m.group(1) if m else None


class Master:
    """The core agent loop.

    The model acts solely by writing Python code, which the harness executes;
    the output is fed back until the model replies without a code block -- that
    reply is the final answer.
    """

    def __init__(
        self,
        llm: LLMProvider,
        session: Session,
        policy: RulePolicy,
        *,
        tier: Tier = Tier.SMART,
        max_steps: int = 20,
    ):
        self._llm = llm
        self._tier = tier
        self._max_steps = max_steps
        toolbox = Toolbox(session, policy, llm)
        self.namespace: dict = {
            "bash": toolbox.bash,
            "read": toolbox.read,
            "write": toolbox.write,
            "edit": toolbox.edit,
            "search": toolbox.search,
            "http_get": toolbox.http_get,
            "http_post": toolbox.http_post,
            "llm": toolbox.llm,
            "Tier": Tier,
            "session": session,
        }
        self._executor = Executor(self.namespace)

    def run(self, task: str) -> str:
        messages = [Message("user", task)]
        for _ in range(self._max_steps):
            reply = self._llm.complete(SYSTEM_PROMPT, messages, self._tier)
            messages.append(Message("assistant", reply))
            code = extract_code(reply)
            if code is None:
                return reply
            result = self._executor.run(code)
            messages.append(Message("user", result.feedback()))
        return "(stopped: reached max_steps)"
