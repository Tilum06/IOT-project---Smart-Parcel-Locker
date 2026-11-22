from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)


class RoomInput(Input):
    room_code: str = Field(pattern=r'^[0-9]+$', min_length=1, max_length=20)


class ResidentInput(RoomInput):
    phone: str = Field(pattern=r'^\+?[0-9]{8,15}$')
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=128)


class Login(Input):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class DepositInput(RoomInput):
    recipient_phone: str = Field(pattern=r'^\+?[0-9]{8,15}$')
    parcel_size: Literal['S', 'M']


class KioskSessionInput(Input):
    parcel_id: str


class PickupInput(Input):
    session_id: str
    code: str = Field(min_length=1, max_length=128)


class AllocationOutput(BaseModel):
    parcel_id: str
    transaction_id: str
    locker_id: str
    requested_size: Literal['S', 'M']
    allocated_size: Literal['S', 'M']
    status: str
