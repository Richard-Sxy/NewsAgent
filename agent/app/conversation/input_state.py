"""负责把原问题+已领取补充转变为不可变快照，这里不会覆盖数据库中的 user_content"""
from dataclasses import dataclass
from uuid import UUID
from app.conversation.confirmation import CONFIRMATION_PREFIX

@dataclass(frozen=True)
class InputText:
    id: UUID
    seq: int
    content: str

@dataclass(frozen=True)
class TurnInputSnapshot:
    original: str
    inputs: tuple[InputText, ...] = ()

    @classmethod
    def from_turn(cls, turn):
        return cls(turn.user_content).with_inputs(item for item in turn.inputs if item.status == "applied")

    def with_inputs(self, values):
        by_id = {item.id: item for item in self.inputs}

        for value in values:
            if value.status != "applied":
                raise ValueError("only applied inputs can enter a snapshot")

            item = InputText(value.id, value.seq, value.content)
            old = by_id.get(item.id)
            if old is not None and old != item:
                raise ValueError("input content cannot be rewrite")
            by_id[item.id] = item

        ordered = tuple(sorted(by_id.values(), key=lambda item: item.seq))
        if len({item.seq for item in ordered}) != len(ordered):
            raise ValueError("input sequence must be unique")

        return TurnInputSnapshot(self.original, ordered)

    @property
    def input_seq(self):
        return self.inputs[-1].seq if self.inputs else 0

    def message(self):
        return self.original + "".join("\n\n补充要求：" + (
            "用户已提交本轮能力确认操作。" if item.content.startswith(CONFIRMATION_PREFIX) else item.content
        ) for item in self.inputs)
