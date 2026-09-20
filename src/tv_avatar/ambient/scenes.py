"""The scenes the avatar can put on. One id per scene, shared with the TV app.

The TV app holds the Director prompt for each id (frontend/src/ambient/scenes.ts);
this side holds only what the model needs to pick one. A scene added here without a
prompt over there fails the TV app's type check, because its scene table is typed
from the generated contract.
"""
from dataclasses import dataclass
from typing import Literal

SceneId = Literal["garden", "beach", "fireplace"]


@dataclass(frozen=True)
class Scene:
    id: SceneId
    label: str
    #: What the viewer is likely to say, worded for the capability manifest.
    hint: str


SCENES: tuple[Scene, ...] = (
    Scene("garden", "Birds in a spring garden",
          "spring garden at dawn, blossom, birdsong"),
    Scene("beach", "Waves on a summer beach",
          "gentle ocean waves on a sandy beach, summer daytime"),
    Scene("fireplace", "Fireplace in a snowy cabin",
          "crackling fireplace in a log cabin, snow falling outside, winter night"),
)


def describe_scenes() -> str:
    """One line for the manifest: `garden (spring garden at dawn, ...), beach (...)`."""
    return ", ".join(f"{s.id} ({s.hint})" for s in SCENES)
