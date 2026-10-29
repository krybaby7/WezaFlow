"""Wire contracts only: no target inspection, clipboard access, or OS writes.

Only reservation_decision=reserved with write_authorized=true authorizes one
external writer attempt. Idempotent replays never authorize another write.
Server-issued session_generation binds each request to one lifecycle generation.
 Lost writers remain owned until
engine shutdown or explicit completion; no expiry releases ownership. Completion
asserts the writer has returned/reaped, including delivery_uncertain. A completed
transaction is never auto-retried, and only a fresh session can reserve again.
"""
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

class CommitOutcome(str, Enum):
    INSERTED_VERIFIED = 'inserted_verified'
    INSERTED_UNVERIFIED = 'inserted_unverified'
    NOT_ATTEMPTED_RECOVERABLE = 'not_attempted_recoverable'
    DELIVERY_UNCERTAIN = 'delivery_uncertain'
    CANCELLED = 'cancelled'
    ERROR = 'error'

@dataclass(frozen=True)
class CommitRequest:
    engine_id: str
    protocol_version: int
    session_generation: int
    session_id: str
    transaction_id: str
    capture_id: str
    result_session_id: str

    @classmethod
    def parse(cls, payload, engine_id):
        if not isinstance(payload, dict):
            raise ValueError('invalid_commit')
        if payload.get('engine_id') != engine_id or type(payload.get('protocol_version')) is not int or payload['protocol_version'] != 1:
            raise ValueError('engine_mismatch')
        generation = payload.get('session_generation')
        if type(generation) is not int or generation <= 0:
            raise ValueError('invalid_session_generation')
        fields = {'session_generation': generation}
        for key in ('session_id', 'transaction_id', 'capture_id', 'result_session_id'):
            value = payload.get(key)
            if not isinstance(value, str):
                raise ValueError('invalid_' + key)
            try:
                fields[key] = str(UUID(value))
            except ValueError:
                raise ValueError('invalid_' + key) from None
        return cls(engine_id=engine_id, protocol_version=1, **fields)

@dataclass(frozen=True)
class CommitResult:
    request: CommitRequest
    outcome: CommitOutcome

    @classmethod
    def parse(cls, payload, engine_id):
        request = CommitRequest.parse(payload, engine_id)
        try:
            outcome = CommitOutcome(payload.get('outcome'))
        except (ValueError, TypeError):
            raise ValueError('invalid_outcome') from None
        return cls(request, outcome)
