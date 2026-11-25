"""Request arguments accepted from the desktop page, before a job enters the queue."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .parse import SpecError

Amount = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Identity = Annotated[int, Field(gt=0, le=2**63 - 1)]


class Arguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Item(Arguments):
    name: str
    quantity: Amount | None = None
    unit: str | None = None
    repeat: Annotated[int, Field(ge=1, le=20)] = 1


class Log(Arguments):
    items: list[Item]
    date: str | None = None
    time: str | None = None


class Target(Arguments):
    id: Identity
    on: str


class Edit(Target):
    quantity: Amount
    unit: str | None = None


class Retime(Target):
    time: str
    date: str | None = None


class Undo(Arguments):
    op: Identity


class RecipeName(Arguments):
    name: str


class Recipe(RecipeName):
    items: list[Item]
    servings: Amount = 1


class Sync(Arguments):
    day: str | None = None
    what: Literal["recipes", "week"] | None = None  # in place of a day: the phone's recipe list, or an export of the last seven days


class JobRequest(Arguments):
    kind: str
    args: dict = Field(default_factory=dict)

    def arguments(self) -> dict:
        models = {
            "log": Log,
            "edit": Edit,
            "retime": Retime,
            "remove": Target,
            "undo": Undo,
            "recipe": Recipe,
            "recipe-delete": RecipeName,
            "sync": Sync,
        }
        if self.kind not in models:
            raise SpecError(f"not something mf does: {self.kind}")
        try:
            return models[self.kind].model_validate(self.args).model_dump()
        except ValidationError as error:
            first = error.errors(include_input=False)[0]
            field = ".".join(map(str, first["loc"]))
            raise SpecError(f"{self.kind} {field}: {first['msg']}") from None
