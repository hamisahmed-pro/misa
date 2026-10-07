"""
Misa Bytecode VM — competitive interpreter via bytecode compilation.

Design: Stack-based VM with linear bytecode. Compiles AST to compact
opcodes, then runs a tight dispatch loop. Target: 10-30x faster than
tree-walking on hot paths.

Opcodes are (op, arg) tuples. Arg is an index into consts/names or a jump target.
"""

# Opcodes
LOAD_CONST = 1    # push consts[arg]
LOAD_VAR = 2      # push var by slot arg
STORE_VAR = 3     # pop -> var slot arg
BINARY_ADD = 10
BINARY_SUB = 11
BINARY_MUL = 12
BINARY_DIV = 13
BINARY_MOD = 14
BINARY_AND = 15   # &
BINARY_OR = 16    # |
BINARY_XOR = 17   # ^
BINARY_SHL = 18   # <<
BINARY_SHR = 19   # >>
COMPARE_LT = 20
COMPARE_LE = 21
COMPARE_GT = 22
COMPARE_GE = 23
COMPARE_EQ = 24
COMPARE_NE = 25
JUMP = 30          # pc = arg
JUMP_IF_FALSE = 31 # pop; if falsy: pc = arg
JUMP_IF_TRUE = 32  # pop; if truthy: pc = arg
CALL = 40          # call function with arg args (function on stack)
RETURN = 41        # pop -> return
LIST_GET = 50      # pop idx, pop list -> push list[idx]
LIST_SET = 51      # pop val, pop idx, pop list -> list[idx] = val
BUILD_LIST = 52    # pop arg items -> push list
PRINT = 60         # pop -> print
POP = 61           # pop and discard
DUP = 62           # duplicate top of stack

class BytecodeFunction:
    """Compiled function: opcodes + constants."""
    def __init__(self, name, code, consts, varnames, argcount):
        self.name = name
        self.code = code  # list of (op, arg)
        self.consts = consts
        self.varnames = varnames
        self.argcount = argcount

class Compiler:
    """AST -> BytecodeFunction."""
    
    def __init__(self):
        self.code = []
        self.consts = []
        self.varnames = []
        self.const_map = {}
    
    def add_const(self, v):
        # Use id for unhashable, value for hashable
        try:
            key = ('c', v)
            if key in self.const_map:
                return self.const_map[key]
        except TypeError:
            pass
        idx = len(self.consts)
        self.consts.append(v)
        try:
            self.const_map[('c', v)] = idx
        except TypeError:
            pass
        return idx
    
    def var_slot(self, name):
        if name not in self.varnames:
            self.varnames.append(name)
        return self.varnames.index(name)
    
    def emit(self, op, arg=0):
        self.code.append((op, arg))
        return len(self.code) - 1
    
    def patch_jump(self, pos, target):
        op, _ = self.code[pos]
        self.code[pos] = (op, target)
    
    def compile(self, node):
        """Dispatch to node-specific compiler."""
        method = f'compile_{type(node).__name__}'
        if hasattr(self, method):
            getattr(self, method)(node)
        else:
            raise NotImplementedError(f"Bytecode: no compiler for {type(node).__name__}")

    # Builtins lowered directly to Python callables (via CALL + resolver).
    BUILTINS = {
        'len': len,
        'range': range,
    }

    def _temp(self, base):
        n = getattr(self, '_tmpcount', 0)
        self._tmpcount = n + 1
        return f'${base}{n}'
    
    def compile_IntLit(self, node):
        self.emit(LOAD_CONST, self.add_const(node.value))
    
    def compile_FloatLit(self, node):
        self.emit(LOAD_CONST, self.add_const(node.value))
    
    def compile_StrLit(self, node):
        self.emit(LOAD_CONST, self.add_const(node.value))
    
    def compile_BoolLit(self, node):
        self.emit(LOAD_CONST, self.add_const(node.value))
    
    def compile_Ident(self, node):
        self.emit(LOAD_VAR, self.var_slot(node.name))
    
    def compile_BinOp(self, node):
        self.compile(node.left)
        self.compile(node.right)
        op_map = {
            '+': BINARY_ADD, '-': BINARY_SUB, '*': BINARY_MUL,
            '/': BINARY_DIV, '%': BINARY_MOD,
            '&': BINARY_AND, '|': BINARY_OR, '^': BINARY_XOR,
            '<<': BINARY_SHL, '>>': BINARY_SHR,
            '<': COMPARE_LT, '<=': COMPARE_LE, '>': COMPARE_GT,
            '>=': COMPARE_GE, '==': COMPARE_EQ, '!=': COMPARE_NE,
        }
        if node.op not in op_map:
            raise NotImplementedError(f"Bytecode: no op for {node.op}")
        self.emit(op_map[node.op])
    
    def compile_LetStmt(self, node):
        self.compile(node.value)
        self.emit(STORE_VAR, self.var_slot(node.name))
    
    def compile_AssignStmt(self, node):
        from . import ast as _A
        # Index target: a[i] = v
        if isinstance(node.target, _A.Index):
            self.compile(node.target.obj)
            self.compile(node.target.idx)
            self.compile(node.value)
            self.emit(LIST_SET)
            return
        # Only simple variable assignment otherwise
        if hasattr(node.target, 'name'):
            self.compile(node.value)
            self.emit(STORE_VAR, self.var_slot(node.target.name))
        else:
            raise NotImplementedError("Bytecode: complex assignment target")

    def compile_Index(self, node):
        self.compile(node.obj)
        self.compile(node.idx)
        self.emit(LIST_GET)

    def compile_ListLit(self, node):
        for it in node.items:
            self.compile(it)
        self.emit(BUILD_LIST, len(node.items))

    def compile_Call(self, node):
        # print(x) -> PRINT opcode (single arg; multi-arg joins via str)
        if node.callee is None and node.name == 'print':
            if len(node.args) == 1:
                self.compile(node.args[0])
                self.emit(PRINT)
                # print returns None -> keep stack balanced for ExprStmt POP
                self.emit(LOAD_CONST, self.add_const(None))
                return
            # multi-arg: call Python print builtin
            self.emit(LOAD_CONST, self.add_const(print))
            for a in node.args:
                self.compile(a)
            self.emit(CALL, len(node.args))
            self.emit(POP)
            return
        # builtin -> LOAD_CONST(callable), args, CALL
        if node.callee is None and node.name in self.BUILTINS:
            self.emit(LOAD_CONST, self.add_const(self.BUILTINS[node.name]))
            for a in node.args:
                self.compile(a)
            self.emit(CALL, len(node.args))
            return
        # general: callee expr or named var, then args
        if node.callee is not None:
            self.compile(node.callee)
        else:
            self.emit(LOAD_VAR, self.var_slot(node.name))
        for a in node.args:
            self.compile(a)
        self.emit(CALL, len(node.args))

    def compile_ForStmt(self, node):
        # Desugar: for v in iter: body
        #   $it = iter; $ix = 0; $ln = len($it)
        #   while $ix < $ln: v = $it[$ix]; body; $ix = $ix + 1
        from . import ast as _A
        it = self._temp('it')
        ix = self._temp('ix')
        ln = self._temp('ln')
        self.compile(node.iter)
        self.emit(STORE_VAR, self.var_slot(it))
        self.emit(LOAD_CONST, self.add_const(0))
        self.emit(STORE_VAR, self.var_slot(ix))
        self.emit(LOAD_CONST, self.add_const(len))
        self.emit(LOAD_VAR, self.var_slot(it))
        self.emit(CALL, 1)
        self.emit(STORE_VAR, self.var_slot(ln))
        loop_start = len(self.code)
        # $ix < $ln
        self.emit(LOAD_VAR, self.var_slot(ix))
        self.emit(LOAD_VAR, self.var_slot(ln))
        self.emit(COMPARE_LT)
        jump_false = self.emit(JUMP_IF_FALSE)
        # v = $it[$ix]
        self.emit(LOAD_VAR, self.var_slot(it))
        self.emit(LOAD_VAR, self.var_slot(ix))
        self.emit(LIST_GET)
        self.emit(STORE_VAR, self.var_slot(node.var))
        for stmt in node.body:
            self.compile(stmt)
        # $ix = $ix + 1
        self.emit(LOAD_VAR, self.var_slot(ix))
        self.emit(LOAD_CONST, self.add_const(1))
        self.emit(BINARY_ADD)
        self.emit(STORE_VAR, self.var_slot(ix))
        self.emit(JUMP, loop_start)
        self.patch_jump(jump_false, len(self.code))
    
    def compile_IfStmt(self, node):
        self.compile(node.cond)
        jump_false = self.emit(JUMP_IF_FALSE)
        for stmt in node.then:
            self.compile(stmt)
        if node.els:
            jump_end = self.emit(JUMP)
            self.patch_jump(jump_false, len(self.code))
            for stmt in node.els:
                self.compile(stmt)
            self.patch_jump(jump_end, len(self.code))
        else:
            self.patch_jump(jump_false, len(self.code))
    
    def compile_WhileStmt(self, node):
        loop_start = len(self.code)
        self.compile(node.cond)
        jump_false = self.emit(JUMP_IF_FALSE)
        for stmt in node.body:
            self.compile(stmt)
        self.emit(JUMP, loop_start)
        self.patch_jump(jump_false, len(self.code))
    
    def compile_ReturnStmt(self, node):
        if node.value:
            self.compile(node.value)
        else:
            self.emit(LOAD_CONST, self.add_const(None))
        self.emit(RETURN)
    
    def compile_ExprStmt(self, node):
        self.compile(node.expr)
        self.emit(POP)
    
    def compile_FnDecl(self, node):
        # Compile function body to separate BytecodeFunction
        # For now, store as const and handle in VM
        sub = Compiler()
        for i, (pname, _) in enumerate(node.params):
            sub.var_slot(pname)
        for stmt in node.body:
            sub.compile(stmt)
        # Implicit return None if no explicit return
        sub.emit(LOAD_CONST, sub.add_const(None))
        sub.emit(RETURN)
        fn = BytecodeFunction(
            node.name,
            sub.code, sub.consts, sub.varnames,
            len(node.params)
        )
        self.emit(LOAD_CONST, self.add_const(fn))
        self.emit(STORE_VAR, self.var_slot(node.name))


class VM:
    """Stack-based bytecode VM."""
    
    def __init__(self):
        self.output = []
    
    def run(self, fn: BytecodeFunction, args=None):
        """Run a BytecodeFunction. Returns the result."""
        # Frames: (fn, pc, stack, vars)
        # For simplicity, use recursion for calls (will optimize later)
        return self._run_frame(fn, args or [])
    
    def _run_frame(self, fn, args):
        code = fn.code
        consts = fn.consts
        # Variables: list indexed by slot
        vars_ = [None] * len(fn.varnames)
        for i, a in enumerate(args):
            if i < len(vars_):
                vars_[i] = a
        
        stack = []
        pc = 0
        
        # Local bindings for speed
        push = stack.append
        pop = stack.pop
        
        while pc < len(code):
            op, arg = code[pc]
            pc += 1
            
            if op == LOAD_CONST:
                push(consts[arg])
            elif op == LOAD_VAR:
                push(vars_[arg])
            elif op == STORE_VAR:
                vars_[arg] = pop()
            elif op == BINARY_ADD:
                b = pop(); a = pop()
                push(a + b)
            elif op == BINARY_SUB:
                b = pop(); a = pop()
                push(a - b)
            elif op == BINARY_MUL:
                b = pop(); a = pop()
                push(a * b)
            elif op == BINARY_DIV:
                b = pop(); a = pop()
                push(a / b)
            elif op == BINARY_MOD:
                b = pop(); a = pop()
                push(a % b)
            elif op == BINARY_AND:
                b = pop(); a = pop()
                push(a & b)
            elif op == BINARY_OR:
                b = pop(); a = pop()
                push(a | b)
            elif op == BINARY_XOR:
                b = pop(); a = pop()
                push(a ^ b)
            elif op == BINARY_SHL:
                b = pop(); a = pop()
                push(a << b)
            elif op == BINARY_SHR:
                b = pop(); a = pop()
                push(a >> b)
            elif op == COMPARE_LT:
                b = pop(); a = pop()
                push(a < b)
            elif op == COMPARE_LE:
                b = pop(); a = pop()
                push(a <= b)
            elif op == COMPARE_GT:
                b = pop(); a = pop()
                push(a > b)
            elif op == COMPARE_GE:
                b = pop(); a = pop()
                push(a >= b)
            elif op == COMPARE_EQ:
                b = pop(); a = pop()
                push(a == b)
            elif op == COMPARE_NE:
                b = pop(); a = pop()
                push(a != b)
            elif op == JUMP:
                pc = arg
            elif op == JUMP_IF_FALSE:
                if not pop():
                    pc = arg
            elif op == JUMP_IF_TRUE:
                if pop():
                    pc = arg
            elif op == CALL:
                # pop args, pop function, call, push result
                f_args = [pop() for _ in range(arg)]
                f_args.reverse()
                func = pop()
                if isinstance(func, BytecodeFunction):
                    push(self._run_frame(func, f_args))
                else:
                    # Native function
                    push(func(*f_args))
            elif op == RETURN:
                return pop()
            elif op == LIST_GET:
                idx = pop(); lst = pop()
                push(lst[idx])
            elif op == LIST_SET:
                val = pop(); idx = pop(); lst = pop()
                lst[idx] = val
            elif op == BUILD_LIST:
                items = [pop() for _ in range(arg)]
                items.reverse()
                push(items)
            elif op == PRINT:
                v = pop()
                self.output.append(str(v))
                print(v)
            elif op == POP:
                pop()
            elif op == DUP:
                push(stack[-1])
            else:
                raise RuntimeError(f"Unknown opcode {op}")
        
        return None
