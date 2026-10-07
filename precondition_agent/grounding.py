import re
import time

from typing import Any, Dict, List

from precondition_agent.prompts import build_symbol_grounding_prompt
from precondition_agent.schemas import GroundedSymbol, RetrievedContext


class GroundingMixin:
    def _context_evidence_for_symbol(
        self,
        symbol: str,
        grouped_contexts: Dict[str, List[RetrievedContext]],
    ) -> List[str]:
        return [
            f"{ctx.path}:{ctx.start_line}-{ctx.end_line}"
            for ctx in grouped_contexts.get(symbol, [])[:2]
        ]

    def _fallback_grounded_meaning(self, symbol: str, kind: str, contexts: List[RetrievedContext]) -> str:
        if not contexts:
            return symbol

        first = contexts[0]
        code = first.code
        header = " ".join(line.strip() for line in code.splitlines()[:4])

        if kind == "type":
            implements_match = re.search(r"\bimplements\s+([^{]+)", header)
            extends_match = re.search(r"\bextends\s+([^{]+)", header)
            kotlin_super_match = re.search(r"\b(class|interface|object)\s+\w+\s*:\s*([^{]+)", header)
            if implements_match:
                implements_text = implements_match.group(1).strip().rstrip("{").strip()
                return f"a type implementing {implements_text}"
            if extends_match:
                extends_text = extends_match.group(1).strip().rstrip("{").strip()
                return f"a type extending {extends_text}"
            if kotlin_super_match:
                super_text = kotlin_super_match.group(2).strip().rstrip("{").strip()
                return f"a Kotlin type extending or implementing {super_text}"
            if "interface" in header:
                return "an interface type used by the PSI/refactoring framework"
            if "object " in header:
                return "a Kotlin singleton object used by the refactoring logic"
            if "class" in header:
                return "a framework class used by the PSI/refactoring logic"

        if kind == "method":
            first_line = next((line.strip() for line in code.splitlines() if line.strip()), symbol)
            if re.search(r"\bfun\s+[A-Za-z_][A-Za-z0-9_<>?.]*\.", first_line):
                return f"a Kotlin extension helper defined as `{first_line}`"
            if "fun " in first_line:
                return f"a Kotlin helper function defined as `{first_line}`"
            return f"a helper method defined as `{first_line}`"

        if kind in {"field", "property"}:
            first_line = next((line.strip() for line in code.splitlines() if line.strip()), symbol)
            if re.search(r"\b(val|var)\s+", first_line):
                return f"a Kotlin property defined as `{first_line}`"
            return f"a state/property value referenced by the precondition"

        return symbol

    def _fallback_ground_symbols(
        self,
        symbols: List[Dict[str, Any]],
        contexts: List[RetrievedContext],
    ) -> List[GroundedSymbol]:
        grouped: Dict[str, List[RetrievedContext]] = {}
        for ctx in contexts:
            grouped.setdefault(ctx.symbol, []).append(ctx)

        grounded: List[GroundedSymbol] = []
        for item in symbols:
            symbol = str(item.get("symbol", "")).strip()
            kind = str(item.get("kind", "")).strip()
            symbol_contexts = grouped.get(symbol, [])
            evidence = self._context_evidence_for_symbol(symbol, grouped)
            grounded.append(
                GroundedSymbol(
                    symbol=symbol,
                    kind=kind,
                    grounded_meaning=self._fallback_grounded_meaning(symbol, kind, symbol_contexts),
                    role_in_snippet=str(item.get("reason", "")).strip() or "Relevant semantic boundary in the snippet",
                    evidence=evidence,
                    confidence="low" if not symbol_contexts else "medium",
                )
            )
        return grounded

    def ground_symbols(
        self,
        probe_result: Dict[str, Any],
        contexts: List[RetrievedContext],
    ) -> List[GroundedSymbol]:
        started_at = time.time()
        symbols = probe_result.get("unknown_symbols", [])
        if not isinstance(symbols, list):
            symbols = probe_result.get("symbols", []) if isinstance(probe_result.get("symbols", []), list) else []
        if not symbols:
            self._trace("symbol_grounding", {"symbols_count": 0, "grounded_count": 0}, started_at)
            return []

        grouped: Dict[str, List[RetrievedContext]] = {}
        for ctx in contexts:
            grouped.setdefault(ctx.symbol, []).append(ctx)

        payload = []
        for item in symbols:
            symbol = str(item.get("symbol", "")).strip()
            payload.append(
                {
                    "symbol": symbol,
                    "kind": item.get("kind", ""),
                    "reason": item.get("reason", ""),
                    "contexts": [
                        {
                            "path": ctx.path,
                            "range": f"{ctx.start_line}-{ctx.end_line}",
                            "code": ctx.code,
                        }
                        for ctx in grouped.get(symbol, [])[:2]
                    ],
                }
            )

        prompt = build_symbol_grounding_prompt(self._current_snippet, payload)

        fallback_items = [self._serialize_grounded_symbol(item) for item in self._fallback_ground_symbols(symbols, contexts)]
        result = self._llm_json(prompt, {"grounded_symbols": fallback_items})

        grounded_symbols: List[GroundedSymbol] = []
        for raw_item in result.get("grounded_symbols", []):
            if not isinstance(raw_item, dict):
                continue
            symbol = str(raw_item.get("symbol", "")).strip()
            if not symbol:
                continue
            grounded_meaning = str(raw_item.get("grounded_meaning", "")).strip()
            if not grounded_meaning or grounded_meaning.lower() == symbol.lower():
                fallback = next((item for item in self._fallback_ground_symbols(symbols, contexts) if item.symbol == symbol), None)
                if fallback:
                    grounded_symbols.append(fallback)
                    continue
            evidence = self._context_evidence_for_symbol(symbol, grouped)
            grounded_symbols.append(
                GroundedSymbol(
                    symbol=symbol,
                    kind=str(raw_item.get("kind", "")).strip(),
                    grounded_meaning=grounded_meaning,
                    role_in_snippet=str(raw_item.get("role_in_snippet", "")).strip(),
                    evidence=evidence if evidence else raw_item.get("evidence", []) if isinstance(raw_item.get("evidence", []), list) else [],
                    confidence=str(raw_item.get("confidence", "medium")).strip() or "medium",
                )
            )

        if not grounded_symbols:
            grounded_symbols = self._fallback_ground_symbols(symbols, contexts)

        self._trace(
            "symbol_grounding",
            {
                "symbols_count": len(symbols),
                "grounded_count": len(grounded_symbols),
                "grounded_symbols": [item.symbol for item in grounded_symbols],
            },
            started_at,
        )
        return grounded_symbols

