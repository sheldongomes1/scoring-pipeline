"""Tool dispatch registry — the `{tool_name -> callable}` seam ADR-3 deferred.

The agent picks a tool by NAME at run-time (ADR-1: the model owns the next edge).
Something has to turn that name + JSON input into an actual Python call and a
tool_result string. That's this file.

Design rule: the registry is GENERIC. It knows nothing about dates, tickers, or
FeatureResult — every tool-specific concern (how to parse the input, how to
serialize the output for the model) is supplied by the tool as a `ToolBinding`.
Add a second tool later = add one more binding; the loop and registry don't change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolBinding:
    """Everything the registry needs to serve ONE tool, supplied by that tool.

    Bundling these per tool keeps the registry generic: the registry calls them; it
    never contains a `if name == "feature_history"`. `definition` is the FULLY-BUILT
    Claude schema — the tool's enum vocabulary (feature keys, section names, ...) is
    baked in by whoever constructs the binding, so the registry holds NO vocabulary
    of its own (ADR-7: the second tool proved a shared `feature_keys` was a hidden
    structured-only assumption).
    """

    name: str
    callable: Callable[..., list]        # the typed Python function (or fake)
    definition: dict                     # fully-built Claude tool-use schema (enum already baked in)
    parse_input: Callable[[dict], dict]  # model JSON input -> kwargs for `callable`
    serialize: Callable[[list], str]     # results -> generator-facing tool_result content


@dataclass(frozen=True)
class DispatchOutcome:
    """The two halves of a tool run, kept separate on purpose (ADR-5).

    `model_content` is the generator-facing subset that goes back as the
    tool_result. `raw_results` is the full envelope (incl. provenance) retained
    for the judge — it never reaches the model. This split is the whole point of
    ADR-5: generator sees what it needs to choose; judge gets the paper trail.
    """

    model_content: str
    raw_results: list[Any]


class ToolRegistry:
    """Holds the bindings; produces the tool menu; routes a call by name."""

    def __init__(self, bindings: list[ToolBinding]) -> None:
        self._bindings = {b.name: b for b in bindings}

    def tool_definitions(self) -> list[dict]:
        """The `tools=[...]` menu sent to Claude — one definition per bound tool."""
        return [b.definition for b in self._bindings.values()]

    def dispatch(self, name: str, model_input: dict) -> DispatchOutcome:
        """Run the named tool on the model's input; return (model_content, raw_results).

        A tool_use for an unknown name is a caller/schema bug, not model choice —
        `strict:true` + the tool menu constrain the model to bound names — so we
        fail loud rather than silently returning an empty result the model would
        misread as 'no data'.
        """
        binding = self._bindings.get(name)
        if binding is None:
            raise KeyError(f"no tool bound for name {name!r}; bound: {sorted(self._bindings)}")
        kwargs = binding.parse_input(model_input)      # seam (a): JSON -> typed args
        raw_results = binding.callable(**kwargs)       # the actual tool call
        model_content = binding.serialize(raw_results)  # ADR-5: generator-facing subset only
        return DispatchOutcome(model_content=model_content, raw_results=raw_results)
