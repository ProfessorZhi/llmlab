"""静态保证：把"本模块不做某事"从纪律变成**结构性事实**。

前面几组测试证明的是行为：给定一个请求，得到的响应是对的吗。这一组证明的是**代码里
根本没有那条路径**：

* 没有网络客户端、没有子进程、没有 GPU 库、没有 ``ctypes`` —— 因此"评测中心不碰
  GPU、不调 EvalScope、不连 :9000"不是靠自觉，而是没有可用的手段；
* 没有磁盘写入与删除 —— 因此它不会动到别人的文件；
* 没有出现真实主机路径、``usage.jsonl``、``.ssh``、权重路径 —— 因此它不会读真实数据；
* 没有跨包 import ``backend.registry`` / ``backend.auth_linux`` —— 那条缝是鸭子类型的，
  不是编译期耦合；
* 生产端口 ``9000`` 只出现在 ``runner.py`` 的**绊线**里，别的文件里连字符串都没有。

扫描用 ``ast`` 而不是字符串匹配：一个在 docstring 里写"没有 subprocess"的模块，不应该
被判成"用了 subprocess"。反过来，字符串常量（也就是真正会被用作路径/URL 的东西）会被
逐字检查，docstring 除外。
"""

from __future__ import annotations

import ast
import unittest

from tests.evaluation.support import import_names, package_files, read_source

#: 一旦出现在本包里，就意味着"评测中心获得了它不该有的能力"。
FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "socketserver",
        "http.client",
        "http.server",
        "urllib.request",
        "urllib.error",
        "urllib.robotparser",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "telnetlib",
        "smtplib",
        "paramiko",
        "pynvml",
        "nvidia_smi",
        "torch",
        "transformers",
        "tensorflow",
        "jax",
        "ctypes",
        "cffi",
        "pickle",
        "marshal",
        "yaml",
    }
)

#: 跨包 import：本包的缝是鸭子类型的（见 ``docs/evalscope-center.md``），不是编译期耦合。
FORBIDDEN_PACKAGES = ("backend.registry", "backend.auth_linux", "backend.atlas", "backend.auth")

#: 会改变磁盘或执行任意代码的调用。本包只读。
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "open", "input"})

#: 无条件禁止的属性调用：它们改盘、改权限或发信号。
UNAMBIGUOUSLY_DESTRUCTIVE = frozenset(
    {
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rmtree",
        "makedirs",
        "mkdir",
        "touch",
        "write_text",
        "write_bytes",
        "chmod",
        "chown",
        "kill",
        "truncate",
    }
)

#: 只有在**接收者**是进程/系统模块时才禁止。
#:
#: ``.run()`` 单独拿出来看完全无害——``self._runner.run(job)`` 是本包自己的方法。
#: 危险的是 ``subprocess.run`` / ``os.system`` 这一类，因此判据是接收者，不是属性名。
PROCESS_ATTRS = frozenset(
    {"system", "popen", "Popen", "run", "call", "check_call", "check_output", "spawnv", "execv"}
)
PROCESS_MODULES = frozenset({"subprocess", "os", "shutil", "signal", "psutil", "pty"})


def receiver_root(node: ast.AST) -> str | None:
    """从 ``a.b.c(...)`` 里取出最左边的名字 ``a``。"""
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Name):
            return current.id
        else:
            return None

#: 真实主机目录前缀。源码里出现以它们开头的字面量，就意味着有文件路径被写死。
#: ``/api/...`` 这类 URL 路由路径**不**在此列。
FILESYSTEM_PREFIXES = (
    "/home",
    "/srv",
    "/opt",
    "/etc",
    "/var",
    "/tmp",
    "/usr",
    "/root",
    "/proc",
    "/dev",
    "/mnt",
    "/media",
    "/run",
    "/boot",
)

#: 真实主机路径与真实数据文件名。出现在**字符串常量**里就是问题。
REAL_DATA_MARKERS = (
    "/home/alice",
    "/home/dave",
    "/home/bob",
    "/home/carol",
    "/srv/llm-lab",
    "/opt/strata",
    "usage.jsonl",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
    ".bash_history",
    "/etc/shadow",
    "/etc/passwd",
)


def sources():
    return [(path, read_source(path)) for path in package_files()]


def trees():
    for path, text in sources():
        yield path, ast.parse(text, filename=str(path))


def docstring_nodes(tree: ast.AST) -> set[int]:
    """模块 / 类 / 函数 docstring 对应的常量节点 id。

    排除它们，才能让"这个模块文档里写了'不读 usage.jsonl'"不被当成"引用了 usage.jsonl"。
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    found.add(id(first.value))
    return found


def string_constants(tree: ast.AST):
    """产出 ``(lineno, value)``：所有字符串常量，docstring 除外。"""
    skip = docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in skip:
                continue
            yield node.lineno, node.value


class ImportSurfaceTests(unittest.TestCase):
    def test_no_module_imports_a_forbidden_capability(self) -> None:
        for path, tree in trees():
            imported = import_names(tree)
            with self.subTest(module=path.name):
                offending = sorted(
                    name
                    for name in imported
                    if name in FORBIDDEN_IMPORTS
                    or any(name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS)
                )
                self.assertEqual(offending, [], msg=f"{path.name} imports {offending}")

    def test_no_module_imports_a_sibling_backend_package(self) -> None:
        """接缝是鸭子类型的：``RequesterContext.from_foreign`` 与 ``TargetCatalog`` 的形状。

        跨包 import 会让"registry 尚未合并"变成"评测中心无法启动"，而本包真正需要的
        只是同一个契约的形状。
        """
        for path, tree in trees():
            imported = import_names(tree)
            with self.subTest(module=path.name):
                offending = sorted(
                    name
                    for name in imported
                    if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_PACKAGES)
                )
                self.assertEqual(offending, [])

    def test_the_package_imports_only_the_standard_library(self) -> None:
        """本包**没有第三方依赖**：装不上它就不该跑不起来。"""
        allowed_prefixes = ("backend.evaluation", "dev.", "tests.")
        for path, tree in trees():
            with self.subTest(module=path.name):
                for name in import_names(tree):
                    if name.startswith(".") or name in ("__future__",):
                        continue
                    if name.startswith(allowed_prefixes):
                        continue
                    root = name.split(".")[0]
                    # 标准库的顶层包：由 Python 自己提供，不是 pip 装来的。
                    self.assertIn(
                        root,
                        {
                            "argparse", "ast", "collections", "contextlib", "dataclasses",
                            "datetime", "hashlib", "http", "io", "json", "os", "pathlib",
                            "re", "secrets", "sqlite3", "sys", "threading", "typing",
                            "urllib", "uuid", "wsgiref", "abc", "enum", "functools",
                            "itertools", "math", "textwrap", "warnings",
                        },
                        msg=f"{path.name} imports non-stdlib {name!r}",
                    )

    def test_no_module_reads_evaluates_or_executes_code(self) -> None:
        for path, tree in trees():
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotIn(
                            node.func.id, FORBIDDEN_CALLS, msg=f"{path.name}:{node.lineno}"
                        )

    def test_no_module_calls_a_destructive_attribute(self) -> None:
        for path, tree in trees():
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                        self.assertNotIn(
                            node.func.attr,
                            UNAMBIGUOUSLY_DESTRUCTIVE,
                            msg=f"{path.name}:{node.lineno}",
                        )

    def test_no_module_spawns_a_process(self) -> None:
        """判据是接收者而不是属性名：``self._runner.run()`` 是本包自己的方法。"""
        for path, tree in trees():
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                        continue
                    if node.func.attr not in PROCESS_ATTRS:
                        continue
                    root = receiver_root(node.func)
                    self.assertNotIn(
                        root,
                        PROCESS_MODULES,
                        msg=f"{path.name}:{node.lineno} calls {root}.{node.func.attr}()",
                    )


class RealDataTests(unittest.TestCase):
    def test_no_real_host_path_or_data_file_appears_as_a_string_literal(self) -> None:
        for path, tree in trees():
            with self.subTest(module=path.name):
                for lineno, value in string_constants(tree):
                    for marker in REAL_DATA_MARKERS:
                        self.assertNotIn(
                            marker, value, msg=f"{path.name}:{lineno} references {marker!r}"
                        )

    def test_no_module_constructs_a_path_under_a_real_host_directory(self) -> None:
        """源码里没有指向真实主机目录的文件路径字面量。

        包内确实有一批以 ``/`` 开头的字面量，但它们全是 **URL 路由路径**
        （``/api/evaluation/...``）与一个相对引用片段（``/attempt-``），不是文件路径。
        因此判据不是"以 / 开头"，而是"以某个真实主机目录开头"——再加上不许出现 ``..``。

        （这条测试的前一版写的是 ``assertNotIn("/", value[1:2])``，即"第二个字符不是
        ``/``"：``"/etc/passwd"`` 的第二个字符是 ``e``，照样通过——一条什么也没挡住的检查。）
        """
        for path, tree in trees():
            with self.subTest(module=path.name):
                for lineno, value in string_constants(tree):
                    for prefix in FILESYSTEM_PREFIXES:
                        self.assertFalse(
                            value.startswith(prefix),
                            msg=f"{path.name}:{lineno} looks like a file path -> {value!r}",
                        )
                    # ``..`` 只在**看起来像绝对路径**的字面量里才是问题：
                    # ``reports.py`` 里那个孤立的 ``".."`` 是它**拒绝**的段，
                    # 而 ``"(0..1439)"`` 是错误信息里的范围写法。
                    if value.startswith("/"):
                        self.assertNotIn(
                            "..",
                            value,
                            msg=f"{path.name}:{lineno} contains a traversal -> {value!r}",
                        )


class ProductionPortTests(unittest.TestCase):
    def test_the_production_port_appears_in_exactly_one_module(self) -> None:
        """``9000`` 是**绊线**，不是配置：它只该出现在 ``runner.py`` 里。"""
        carriers = []
        for path, text in sources():
            if "9000" in text:
                carriers.append(path.name)
        self.assertEqual(carriers, ["runner.py"])

    def test_the_tripwire_constant_is_the_production_port(self) -> None:
        from backend.evaluation.runner import EVALSCOPE_PRODUCTION_PORT

        self.assertEqual(EVALSCOPE_PRODUCTION_PORT, 9000)

    def test_no_module_builds_a_url_with_that_port(self) -> None:
        from backend.evaluation.errors import InvalidRequest
        from backend.evaluation.runner import parse_runner_base_url

        with self.assertRaises(InvalidRequest):
            parse_runner_base_url("http://127.0.0.1:9000")


class StructuralInvariantTests(unittest.TestCase):
    def test_the_mock_runner_is_structurally_synthetic_only(self) -> None:
        from backend.evaluation.runner import MockRunner

        self.assertIs(MockRunner.synthetic_only, True)
        self.assertIs(MockRunner.is_real, False)

    def test_the_mock_runner_hard_codes_synthetic(self) -> None:
        """``run()`` 里 ``synthetic`` 是常量，不是参数——没有一条路径能让它变成 False。

        扫描**只**针对 ``MockRunner.run``：``_EvalScopeAdapterBase.run`` 也叫 ``run``，
        但它是"一律拒绝"的那一个，不构造任何样本。
        """
        path = next(p for p in package_files() if p.name == "runner.py")
        tree = ast.parse(read_source(path), filename=str(path))

        mock_run = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MockRunner":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "run":
                        mock_run = item
        self.assertIsNotNone(mock_run, "MockRunner.run was not found")

        calls = [
            inner
            for inner in ast.walk(mock_run)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "RunnerSample"
        ]
        self.assertTrue(calls, "MockRunner.run must build a RunnerSample")
        for call in calls:
            keywords = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn("synthetic", keywords)
            value = keywords["synthetic"]
            self.assertIsInstance(value, ast.Constant)
            self.assertIs(value.value, True)

    def test_the_mock_routes_are_absent_by_default(self) -> None:
        from backend.evaluation.api import ROUTES, routes_for

        dev_routes = [route for route in ROUTES if route.dev_only]
        self.assertEqual(len(dev_routes), 1)
        self.assertIn("mock-run", dev_routes[0].template)

        default = routes_for(enable_mock_routes=False)
        self.assertNotIn(dev_routes[0], default)
        self.assertEqual(len(default), len(ROUTES) - 1)

    def test_create_app_defaults_to_no_mock_routes(self) -> None:
        import inspect

        from backend.evaluation.api import create_app

        signature = inspect.signature(create_app)
        self.assertIs(signature.parameters["enable_mock_routes"].default, False)

    def test_the_service_defaults_to_the_closed_admission_policy(self) -> None:
        """默认配置本身就是"本部署此刻的样子"：GPU 满、不派发、不读报告内容。"""
        from backend.evaluation.service import EvaluationConfig

        config = EvaluationConfig()
        self.assertFalse(config.admission.dispatch_enabled)
        self.assertFalse(config.admission.require_real_runner is False)
        self.assertTrue(config.admission.window.always_closed)
        self.assertFalse(config.gpu_facts.observed)
        self.assertIsNone(config.gpu_facts.free_mib)
        self.assertFalse(config.report_root.configured)
        self.assertFalse(config.native_entry_available)
        self.assertEqual(config.runner_kind, "mock")
        self.assertTrue(config.mock_runner_enabled)

    def test_the_report_content_reader_is_closed_by_default(self) -> None:
        from backend.evaluation.errors import NotConfigured
        from backend.evaluation.reports import unconfigured_root

        with self.assertRaises(NotConfigured):
            unconfigured_root().read_bytes("runs/a.json")

    def test_the_identity_module_exposes_a_duck_typed_adapter(self) -> None:
        """接缝的证据：一个**外来**对象只要有那几个属性就能被接进来。"""
        from backend.evaluation.identity import RequesterContext

        class ForeignRegistryContext:
            principal = "alice"
            uid = 4001
            linux_user = "alice"
            scopes = ("registry:read", "evaluation:read")
            roles = ("member",)
            token_label = "synthetic"

        adapted = RequesterContext.from_foreign(ForeignRegistryContext())
        self.assertEqual(adapted.principal, "alice")
        self.assertEqual(adapted.uid, 4001)
        self.assertTrue(adapted.has_scope("evaluation:read"))

    def test_the_adapter_refuses_an_object_that_cannot_supply_a_principal(self) -> None:
        from backend.evaluation.errors import InvalidRequest
        from backend.evaluation.identity import RequesterContext

        class Incomplete:
            uid = 4001

        with self.assertRaises(InvalidRequest):
            RequesterContext.from_foreign(Incomplete())


class TestSuiteHygieneTests(unittest.TestCase):
    """测试自己也不能读真实数据：证据不该来自真实报告、提示词或用户主目录。

    这两条断言检查的是**文件访问**，不是字符串里是否出现过某个词。测试必须能**说出**
    ``prompt``（才能断言它被拒绝）、必须能**写出** ``/home/``（才能断言它不出现在响应里），
    所以"提到了这个词"不是违反——"打开了这个文件"才是。

    本模块自身被排除在扫描之外：它持有的 :data:`REAL_DATA_MARKERS` 列表**必须**逐字
    包含这些标记，否则它就扫描不了别的东西。
    """

    SELF = "test_static_guarantees.py"

    def _test_files(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent
        return [
            path
            for path in sorted(root.glob("test_*.py")) + [root / "support.py"]
            if path.name != self.SELF
        ]

    def test_no_test_file_opens_a_file_by_path(self) -> None:
        """整个套件不调用 ``open()``：它只用 pathlib 与 tempfile 里自己造的东西。"""
        for path in self._test_files():
            tree = ast.parse(read_source(path), filename=str(path))
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        self.assertNotEqual(node.func.id, "open", msg=f"{path.name}:{node.lineno}")

    def test_no_test_constructs_a_path_under_a_real_host_directory(self) -> None:
        """没有任何测试去构造 ``/home/<user>`` / ``/srv`` / ``/opt`` 下的路径。"""
        forbidden_prefixes = ("/home/", "/srv/", "/opt/", "~/.ssh", "~/prompts")
        for path in self._test_files():
            tree = ast.parse(read_source(path), filename=str(path))
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name not in ("Path", "open"):
                        continue
                    for argument in node.args:
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                            for prefix in forbidden_prefixes:
                                self.assertFalse(
                                    argument.value.startswith(prefix),
                                    msg=f"{path.name}:{node.lineno} builds {argument.value!r}",
                                )

    def test_no_test_file_reads_a_jsonl_line_file(self) -> None:
        """``.jsonl`` 是真实用量日志的形态；测试里没有任何路径以它结尾。"""
        for path in self._test_files():
            tree = ast.parse(read_source(path), filename=str(path))
            with self.subTest(module=path.name):
                for lineno, value in string_constants(tree):
                    self.assertFalse(
                        value.endswith(".jsonl"),
                        msg=f"{path.name}:{lineno} -> {value!r}",
                    )

    def test_no_test_reads_a_report_from_the_real_report_root(self) -> None:
        """唯一被 ``read_bytes`` 的引用是**本部署关闭**的那条路径。"""
        for path in self._test_files():
            tree = ast.parse(read_source(path), filename=str(path))
            with self.subTest(module=path.name):
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr == "read_bytes":
                        self.assertIn(
                            path.name, ("test_reports.py",), msg=f"{path.name}:{node.lineno}"
                        )

    def test_the_static_scan_actually_sees_the_package(self) -> None:
        """一条元断言：如果扫描目标为空，前面所有的"没有违反"都是空的。"""
        names = {path.name for path in package_files()}
        for expected in (
            "api.py", "service.py", "store.py", "runner.py", "admission.py",
            "identity.py", "authz.py", "dto.py", "openapi.py", "cli.py",
            "evaluation_preview.py",
        ):
            with self.subTest(module=expected):
                self.assertIn(expected, names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
