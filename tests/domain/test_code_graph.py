"""
Unit Tests for CodeGraph Domain Entities
=========================================
Kiểm thử CodeNodeType, CodeFunctionNode, ImportInfo.
"""

import pytest
from dataclasses import dataclass
from kaos.domain.code_graph import CodeNodeType, CodeFunctionNode, ImportInfo


class TestCodeNodeType:
    """Kiểm thử enum CodeNodeType — tránh bug 'constructor' tái phát (P1 fix)."""

    def test_has_constructor_member(self):
        """CONSTRUCTOR đã tồn tại trong enum (regression: bug P1)."""
        assert CodeNodeType.CONSTRUCTOR == "constructor"
        assert CodeNodeType.CONSTRUCTOR in CodeNodeType

    def test_has_all_expected_members(self):
        """Tất cả node type đã có mặt."""
        expected = {"function", "method", "arrow_function", "class", "constructor"}
        actual = {m.value for m in CodeNodeType}
        assert actual == expected

    def test_from_string_valid(self):
        """Chuyển string hợp lệ thành enum member."""
        assert CodeNodeType("function") == CodeNodeType.FUNCTION
        assert CodeNodeType("constructor") == CodeNodeType.CONSTRUCTOR
        assert CodeNodeType("class") == CodeNodeType.CLASS

    def test_from_string_invalid_raises(self):
        """String không hợp lệ raise ValueError."""
        with pytest.raises(ValueError):
            CodeNodeType("invalid_type")
        with pytest.raises(ValueError):
            CodeNodeType("")


class TestImportInfo:
    """Kiểm thử ImportInfo dataclass."""

    def test_create_import_info(self):
        imp = ImportInfo(module="@stax/contracts", imported_names=["CreateUserDto", "LoginDto"])
        assert imp.module == "@stax/contracts"
        assert imp.imported_names == ["CreateUserDto", "LoginDto"]

    def test_empty_imported_names(self):
        imp = ImportInfo(module="@stax/utils", imported_names=[])
        assert imp.imported_names == []

    def test_equality(self):
        a = ImportInfo(module="m", imported_names=["a"])
        b = ImportInfo(module="m", imported_names=["a"])
        assert a == b

    def test_repr(self):
        imp = ImportInfo(module="m", imported_names=["a"])
        r = repr(imp)
        assert "ImportInfo" in r
        assert "m" in r


class TestCodeFunctionNode:
    """Kiểm thử CodeFunctionNode dataclass — defaults, optional fields, validation."""

    def test_required_fields_only(self):
        """Tạo node chỉ với required fields — các optional phải có default đúng."""
        node = CodeFunctionNode(
            function_name="createUser",
            file_path="packages/backend/src/modules/users/user.service.ts",
            start_line=45,
            end_line=78,
            is_exported=True,
            is_async=False,
        )
        assert node.function_name == "createUser"
        assert node.file_path == "packages/backend/src/modules/users/user.service.ts"
        assert node.start_line == 45
        assert node.end_line == 78
        assert node.is_exported is True
        assert node.is_async is False
        # Defaults
        assert node.node_type == CodeNodeType.FUNCTION
        assert node.class_name is None
        assert node.access_modifier == "public"
        assert node.imports == []
        assert node.callee_functions == []
        assert node.caller_functions == []
        assert node.description == ""
        assert node.preconditions == []
        assert node.exceptions == []
        assert node.side_effects == []
        assert node.keywords == []
        assert node.file_hash == ""
        assert node.last_scanned_at == ""

    def test_with_class_method(self):
        """Node là method trong class."""
        node = CodeFunctionNode(
            function_name="findByFilter",
            file_path="src/repositories/user.repo.ts",
            start_line=10,
            end_line=35,
            is_exported=False,
            is_async=True,
            node_type=CodeNodeType.METHOD,
            class_name="UserRepository",
            access_modifier="private",
        )
        assert node.class_name == "UserRepository"
        assert node.access_modifier == "private"
        assert node.node_type == CodeNodeType.METHOD
        assert node.is_async is True

    def test_with_constructor(self):
        """Constructor node (regression: bug P1)."""
        node = CodeFunctionNode(
            function_name="constructor",
            file_path="src/services/auth.service.ts",
            start_line=5,
            end_line=12,
            is_exported=False,
            is_async=False,
            node_type=CodeNodeType.CONSTRUCTOR,
            class_name="AuthService",
        )
        assert node.node_type == CodeNodeType.CONSTRUCTOR
        assert node.function_name == "constructor"

    def test_with_imports_and_callees(self):
        """Node có imports và callee_functions."""
        imports = [
            ImportInfo(module="@stax/contracts", imported_names=["CreateUserDto"]),
            ImportInfo(module="@nestjs/common", imported_names=["Injectable"]),
        ]
        node = CodeFunctionNode(
            function_name="createUser",
            file_path="src/services/user.service.ts",
            start_line=20,
            end_line=45,
            is_exported=True,
            is_async=True,
            imports=imports,
            callee_functions=["DbRepository.findByFilter", "EmailService.send"],
        )
        assert len(node.imports) == 2
        assert len(node.callee_functions) == 2
        assert "DbRepository.findByFilter" in node.callee_functions

    def test_with_semantic_fields(self):
        """Node có LLM-enriched fields."""
        node = CodeFunctionNode(
            function_name="processPayment",
            file_path="src/services/payment.service.ts",
            start_line=100,
            end_line=150,
            is_exported=True,
            is_async=True,
            description="Xử lý thanh toán qua Stripe",
            preconditions=["user phải đăng nhập", "cart không được empty"],
            exceptions=["PaymentException nếu thẻ hết hạn"],
            side_effects=["Ghi vào bảng payments", "Gửi email xác nhận"],
            keywords=["payment", "stripe", "checkout"],
        )
        assert node.description == "Xử lý thanh toán qua Stripe"
        assert len(node.preconditions) == 2
        assert len(node.exceptions) == 1
        assert len(node.side_effects) == 2
        assert len(node.keywords) == 3

    def test_equality(self):
        """Hai node cùng data phải bằng nhau (dataclass default)."""
        a = CodeFunctionNode(function_name="f", file_path="p", start_line=1, end_line=2, is_exported=True, is_async=False)
        b = CodeFunctionNode(function_name="f", file_path="p", start_line=1, end_line=2, is_exported=True, is_async=False)
        assert a == b

    def test_inequality(self):
        a = CodeFunctionNode(function_name="f", file_path="p", start_line=1, end_line=2, is_exported=True, is_async=False)
        b = CodeFunctionNode(function_name="g", file_path="p", start_line=1, end_line=2, is_exported=True, is_async=False)
        assert a != b

    def test_caller_functions_default_empty(self):
        """caller_functions mặc định là empty list — được điền sau."""
        node = CodeFunctionNode(
            function_name="f",
            file_path="p",
            start_line=1,
            end_line=2,
            is_exported=True,
            is_async=False,
        )
        assert node.caller_functions == []
        # Mutate
        node.caller_functions.append("other.f")
        assert len(node.caller_functions) == 1  # dataclass field không copy on write

    def test_arrow_function_type(self):
        """Arrow function node type."""
        node = CodeFunctionNode(
            function_name="handleClick",
            file_path="src/components/button.tsx",
            start_line=30,
            end_line=32,
            is_exported=False,
            is_async=False,
            node_type=CodeNodeType.ARROW_FUNCTION,
        )
        assert node.node_type == CodeNodeType.ARROW_FUNCTION
        assert node.is_exported is False

    def test_file_hash_metadata(self):
        """file_hash và last_scanned_at."""
        node = CodeFunctionNode(
            function_name="f",
            file_path="p",
            start_line=1,
            end_line=2,
            is_exported=True,
            is_async=False,
            file_hash="abc123",
            last_scanned_at="2026-07-01T08:00:00Z",
        )
        assert node.file_hash == "abc123"
        assert node.last_scanned_at == "2026-07-01T08:00:00Z"
