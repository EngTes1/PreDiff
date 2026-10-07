import re

from pathlib import Path
from typing import Any, Dict, List, Optional

from precondition_agent.language_rules.kotlin_rules import KOTLIN_AST_FEATURE_PATTERNS

try:
    from tree_sitter_language_pack import get_parser as get_tree_sitter_parser
except Exception:
    get_tree_sitter_parser = None


class KotlinAstParser:
    """Tree-sitter based Kotlin AST adapter normalized to the agent's AST shape."""

    def __init__(self) -> None:
        self._parser: Any = None
        self.error: str = ""

    def available(self) -> bool:
        return self._get_parser() is not None

    def parse_file(self, path: str) -> Dict[str, Any]:
        parser = self._get_parser()
        if parser is None:
            raise RuntimeError(self.error or "Kotlin Tree-sitter parser is unavailable")

        source_text = Path(path).read_text(encoding="utf-8", errors="ignore")
        source_bytes = source_text.encode("utf-8", errors="ignore")
        tree = parser.parse(source_bytes)

        parsed: Dict[str, Any] = {
            "language": "kotlin",
            "path": path,
            "types": [],
            "methods": [],
            "properties": [],
        }

        def walk(node: Any, owners: List[str]) -> None:
            if self._is_type_node(node):
                type_item = self._extract_type_item(node, source_bytes)
                next_owners = owners
                if type_item:
                    parsed["types"].append(type_item)
                    next_owners = owners + [type_item.get("name", "")]
                for child in self._named_children(node):
                    walk(child, next_owners)
                return

            if self._is_callable_node(node):
                method_item = self._extract_method_item(node, source_bytes, owners[-1] if owners else "")
                if method_item:
                    parsed["methods"].append(method_item)

            if self._is_property_node(node):
                property_item = self._extract_property_item(node, source_bytes, owners[-1] if owners else "")
                if property_item:
                    parsed["properties"].append(property_item)

            for child in self._named_children(node):
                walk(child, owners)

        walk(tree.root_node, [])
        return parsed

    def _get_parser(self) -> Any:
        if self._parser is not None:
            return self._parser
        if self.error:
            return None
        if get_tree_sitter_parser is None:
            self.error = "tree-sitter-language-pack is not installed"
            return None
        try:
            self._parser = get_tree_sitter_parser("kotlin")
            return self._parser
        except Exception as exc:
            self.error = str(exc)
            return None

    def _named_children(self, node: Any) -> List[Any]:
        named_children = getattr(node, "named_children", None)
        if named_children is not None:
            return list(named_children)
        return [child for child in getattr(node, "children", []) if getattr(child, "is_named", False)]

    def _node_text(self, source_bytes: bytes, node: Any) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

    def _is_type_node(self, node: Any) -> bool:
        node_type = str(getattr(node, "type", ""))
        return node_type in {
            "class_declaration",
            "interface_declaration",
            "object_declaration",
        } or ("class" in node_type and "declaration" in node_type)

    def _is_callable_node(self, node: Any) -> bool:
        node_type = str(getattr(node, "type", ""))
        return node_type in {
            "function_declaration",
            "primary_constructor",
            "secondary_constructor",
            "constructor_declaration",
        } or ("function" in node_type and "declaration" in node_type)

    def _is_property_node(self, node: Any) -> bool:
        node_type = str(getattr(node, "type", ""))
        return node_type in {"property_declaration", "multi_variable_declaration"} or (
            "property" in node_type and "declaration" in node_type
        )

    def _extract_modifiers(self, header: str) -> List[str]:
        known = {
            "public", "private", "protected", "internal", "open", "final", "abstract",
            "sealed", "data", "inner", "enum", "annotation", "value", "inline",
            "tailrec", "operator", "infix", "suspend", "override", "actual", "expect",
        }
        return [token for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", header) if token in known]

    def _extract_type_item(self, node: Any, source_bytes: bytes) -> Optional[Dict[str, Any]]:
        text = self._node_text(source_bytes, node)
        header = text.split("{", 1)[0]
        match = re.search(
            r"\b((?:enum|annotation|data|sealed|value)\s+)?(class|interface|object)\s+([A-Za-z_][A-Za-z0-9_]*)",
            header,
        )
        if not match:
            return None

        kind_word = match.group(2)
        if match.group(1) and "enum" in match.group(1):
            kind = "ENUM"
        elif kind_word == "interface":
            kind = "INTERFACE"
        elif kind_word == "object":
            kind = "OBJECT"
        else:
            kind = "CLASS"

        name = match.group(3)
        start_line = int(node.start_point[0]) + 1
        end_line = int(node.end_point[0]) + 1
        brace_offset = text.find("{")
        if brace_offset >= 0:
            header_end_line = start_line + text[:brace_offset].count("\n")
        else:
            header_end_line = min(end_line, start_line + 3)

        before_keyword = header[:match.start()]
        super_types: List[str] = []
        colon_match = re.search(r":\s*([^{\n]+)", header)
        if colon_match:
            super_text = colon_match.group(1)
            for part in super_text.split(","):
                type_match = re.search(r"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*)\b", part.strip())
                if type_match:
                    super_types.append(type_match.group(1).split(".")[-1])

        return {
            "language": "kotlin",
            "kind": kind,
            "name": name,
            "start_line": start_line,
            "end_line": end_line,
            "header_end_line": header_end_line,
            "modifiers": self._extract_modifiers(before_keyword + " " + (match.group(1) or "")),
            "extends_types": super_types,
            "implements_types": [],
        }

    def _extract_method_item(
        self,
        node: Any,
        source_bytes: bytes,
        owner_type: str,
    ) -> Optional[Dict[str, Any]]:
        text = self._node_text(source_bytes, node)
        node_type = str(getattr(node, "type", ""))
        header = re.split(r"\{|=", text, maxsplit=1)[0]
        start_line = int(node.start_point[0]) + 1
        end_line = int(node.end_point[0]) + 1

        is_constructor = "constructor" in node_type or bool(re.search(r"\bconstructor\s*\(", header))
        extension_receiver = ""
        if is_constructor:
            name = "<init>"
            name_match = re.search(r"\bconstructor\s*\(", header)
            before_name = header[: name_match.start()] if name_match else ""
        else:
            match = re.search(
                r"\bfun\s+(?:<[^>]+>\s*)?(?:(?P<receiver>[A-Za-z_][A-Za-z0-9_<>?.]*(?:\.[A-Za-z_][A-Za-z0-9_<>?.]*)*)\.)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
                header,
            )
            if not match:
                return None
            name = match.group("name")
            extension_receiver = (match.group("receiver") or "").split(".")[-1]
            before_name = header[: match.start()]

        return_type = ""
        return_match = re.search(r"\)\s*:\s*([A-Za-z_][A-Za-z0-9_<>?.]*(?:\.[A-Za-z_][A-Za-z0-9_<>?.]*)*)", header)
        if return_match:
            return_type = return_match.group(1).split(".")[-1]

        parameter_types: List[str] = []
        params_match = re.search(r"\((.*)\)", header, flags=re.DOTALL)
        if params_match:
            parameter_text = params_match.group(1)
            for part in parameter_text.split(","):
                type_match = re.search(r":\s*([A-Za-z_][A-Za-z0-9_<>?.]*(?:\.[A-Za-z_][A-Za-z0-9_<>?.]*)*)", part)
                if type_match:
                    parameter_types.append(type_match.group(1).split(".")[-1])

        called_methods = []
        seen_calls = set()
        for call in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text):
            if call in {"if", "when", "for", "while", "catch", "return", "fun", "constructor"}:
                continue
            if call == name or call in seen_calls:
                continue
            seen_calls.add(call)
            called_methods.append(call)

        instanceof_types = [
            match.split(".")[-1]
            for match in re.findall(r"(?:\bis\b|!is\s+)\s*([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*)", text)
        ]
        cast_types = [
            match.split(".")[-1]
            for match in re.findall(r"\bas\??\s+([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*)", text)
        ]

        type_refs = list(dict.fromkeys(parameter_types + ([return_type] if return_type else []) + instanceof_types + cast_types))
        conditions = self._extract_conditions(text)
        kotlin_features = self._extract_kotlin_features(text)

        return {
            "language": "kotlin",
            "owner_type": owner_type,
            "name": name,
            "is_constructor": is_constructor,
            "is_top_level": not owner_type and not is_constructor,
            "is_extension": bool(extension_receiver),
            "extension_receiver": extension_receiver,
            "start_line": start_line,
            "end_line": end_line,
            "return_type": return_type,
            "parameter_types": parameter_types,
            "modifiers": self._extract_modifiers(before_name),
            "called_methods": called_methods,
            "type_refs": type_refs,
            "instanceof_types": instanceof_types,
            "cast_types": cast_types,
            "conditions": conditions,
            "kotlin_features": kotlin_features,
        }

    def _extract_conditions(self, text: str) -> List[str]:
        conditions: List[str] = []
        for pattern in [
            r"\bif\s*\([^)]*\)",
            r"\bwhen\s*\([^)]*\)",
            r"\breturn\s+[^\n;]+",
            r"\b[A-Za-z_][A-Za-z0-9_.]*\s+as\?\s+[A-Z][A-Za-z0-9_.<>?]*(?:\s*\?:\s*return\s+[^\n;}]*)?",
            r"\b[A-Za-z_][A-Za-z0-9_.]*\s*\?:\s*return\s+[^\n;}]*",
            r"\b[A-Za-z_][A-Za-z0-9_.]*\?\.[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?",
            r"(?:^|[{\s])(?:is|!is)\s+[A-Z][A-Za-z0-9_.<>?]*\s*->",
        ]:
            for match in re.findall(pattern, text):
                compact = " ".join(match.strip().split())
                if re.match(r"^[A-Z][A-Za-z0-9_.<>?]*\s*\?:\s*return\b", compact):
                    continue
                if compact and compact not in conditions:
                    conditions.append(compact[:180])
        return conditions[:12]

    def _extract_property_item(
        self,
        node: Any,
        source_bytes: bytes,
        owner_type: str,
    ) -> Optional[Dict[str, Any]]:
        text = self._node_text(source_bytes, node)
        header = re.split(r"\{|=", text, maxsplit=1)[0]
        match = re.search(
            r"\b(?P<kind>val|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*(?P<type>[A-Za-z_][A-Za-z0-9_<>?.]*(?:\.[A-Za-z_][A-Za-z0-9_<>?.]*)*))?",
            header,
        )
        if not match:
            return None

        type_name = (match.group("type") or "").split(".")[-1]
        start_line = int(node.start_point[0]) + 1
        end_line = int(node.end_point[0]) + 1
        return {
            "language": "kotlin",
            "owner_type": owner_type,
            "name": match.group("name"),
            "kind": match.group("kind"),
            "start_line": start_line,
            "end_line": end_line,
            "return_type": type_name,
            "type_refs": [type_name] if type_name else [],
            "conditions": self._extract_conditions(text),
            "called_methods": [
                call
                for call in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
                if call not in {"if", "when", "get", "set"}
            ],
            "kotlin_features": self._extract_kotlin_features(text),
            "has_getter": bool(re.search(r"\bget\s*\(", text)),
        }

    def _extract_kotlin_features(self, text: str) -> List[str]:
        features: List[str] = []
        for name, pattern in KOTLIN_AST_FEATURE_PATTERNS:
            if re.search(pattern, text, flags=re.DOTALL):
                features.append(name)
        return features
