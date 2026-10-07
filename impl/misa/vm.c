/*
 * Misa Bytecode VM — C dispatch loop
 * 
 * Takes bytecode from Python, runs a tight C switch loop.
 * Values are Python objects (PyObject*), opcodes are ints.
 * 
 * Target: 10-20x faster than Python dispatch loop.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

// Opcodes (must match bytecode.py)
#define LOAD_CONST 1
#define LOAD_VAR 2
#define STORE_VAR 3
#define BINARY_ADD 10
#define BINARY_SUB 11
#define BINARY_MUL 12
#define BINARY_DIV 13
#define BINARY_MOD 14
#define BINARY_AND 15
#define BINARY_OR 16
#define BINARY_XOR 17
#define BINARY_SHL 18
#define BINARY_SHR 19
#define COMPARE_LT 20
#define COMPARE_LE 21
#define COMPARE_GT 22
#define COMPARE_GE 23
#define COMPARE_EQ 24
#define COMPARE_NE 25
#define JUMP 30
#define JUMP_IF_FALSE 31
#define JUMP_IF_TRUE 32
#define CALL 40
#define RETURN 41
#define LIST_GET 50
#define LIST_SET 51
#define BUILD_LIST 52
#define PRINT 60
#define POP 61
#define DUP 62

/* Fast path for exact-PyLong arithmetic. Returns new ref or NULL to
 * indicate the slow path (PyNumber_*) should be used. */
static PyObject* fast_long_add(PyObject *a, PyObject *b) {
    if (!PyLong_CheckExact(a) || !PyLong_CheckExact(b)) return NULL;
    int ovf_a = 0, ovf_b = 0;
    long long av = PyLong_AsLongLongAndOverflow(a, &ovf_a);
    long long bv = PyLong_AsLongLongAndOverflow(b, &ovf_b);
    if (ovf_a || ovf_b) return NULL;
    long long rv = av + bv;
    if ((av ^ bv) >= 0 && (rv ^ av) < 0) return NULL;  /* overflow */
    return PyLong_FromLongLong(rv);
}

static PyObject* fast_long_sub(PyObject *a, PyObject *b) {
    if (!PyLong_CheckExact(a) || !PyLong_CheckExact(b)) return NULL;
    int ovf_a = 0, ovf_b = 0;
    long long av = PyLong_AsLongLongAndOverflow(a, &ovf_a);
    long long bv = PyLong_AsLongLongAndOverflow(b, &ovf_b);
    if (ovf_a || ovf_b) return NULL;
    long long rv = av - bv;
    if ((av ^ bv) < 0 && (rv ^ av) < 0) return NULL;  /* overflow */
    return PyLong_FromLongLong(rv);
}

static PyObject* fast_long_mul(PyObject *a, PyObject *b) {
    if (!PyLong_CheckExact(a) || !PyLong_CheckExact(b)) return NULL;
    int ovf_a = 0, ovf_b = 0;
    long long av = PyLong_AsLongLongAndOverflow(a, &ovf_a);
    long long bv = PyLong_AsLongLongAndOverflow(b, &ovf_b);
    if (ovf_a || ovf_b) return NULL;
    /* overflow check via division */
    long long rv = av * bv;
    if (av != 0 && rv / av != bv) return NULL;
    return PyLong_FromLongLong(rv);
}

/*
 * Run the VM.
 * 
 * Args:
 *   code: list of (op, arg) tuples
 *   consts: list of Python constants
 *   nvars: number of variables
 *   args: tuple of argument values
 * 
 * Returns: result PyObject* (or NULL on error)
 */
static PyObject* vm_run(PyObject* self, PyObject* args) {
    PyObject *code_list, *consts_list, *args_tuple;
    PyObject *resolver = NULL, *output_list = NULL;
    int nvars;

    if (!PyArg_ParseTuple(args, "OOiO|OO", &code_list, &consts_list, &nvars,
                          &args_tuple, &resolver, &output_list)) {
        return NULL;
    }
    
    Py_ssize_t code_len = PyList_Size(code_list);
    if (code_len < 0) return NULL;
    
    // Allocate C arrays for code
    int *ops = (int*)malloc(code_len * sizeof(int));
    int *argvals = (int*)malloc(code_len * sizeof(int));
    if (!ops || !argvals) {
        free(ops); free(argvals);
        return PyErr_NoMemory();
    }
    
    // Convert code list to C arrays
    for (Py_ssize_t i = 0; i < code_len; i++) {
        PyObject *instr = PyList_GetItem(code_list, i);
        if (!PyTuple_Check(instr) || PyTuple_Size(instr) != 2) {
            free(ops); free(argvals);
            PyErr_SetString(PyExc_ValueError, "code must be list of (op, arg)");
            return NULL;
        }
        ops[i] = (int)PyLong_AsLong(PyTuple_GetItem(instr, 0));
        argvals[i] = (int)PyLong_AsLong(PyTuple_GetItem(instr, 1));
        if (PyErr_Occurred()) {
            free(ops); free(argvals);
            return NULL;
        }
    }
    
    // Variables array
    PyObject **vars = (PyObject**)calloc(nvars, sizeof(PyObject*));
    if (!vars) {
        free(ops); free(argvals);
        return PyErr_NoMemory();
    }
    
    // Initialize args
    Py_ssize_t nargs = PyTuple_Size(args_tuple);
    for (Py_ssize_t i = 0; i < nargs && i < nvars; i++) {
        vars[i] = PyTuple_GetItem(args_tuple, i);
        Py_INCREF(vars[i]);
    }
    
    // Value stack (fixed size, should be enough)
    #define STACK_SIZE 1024
    PyObject *stack[STACK_SIZE];
    int sp = 0;  // stack pointer
    
    #define PUSH(v) do { \
        if (sp >= STACK_SIZE) { \
            PyErr_SetString(PyExc_RuntimeError, "stack overflow"); \
            goto error; \
        } \
        stack[sp++] = (v); \
    } while(0)
    
    #define STACK_POP() (stack[--sp])
    
    PyObject *result = NULL;
    Py_ssize_t pc = 0;
    
    // Main dispatch loop
    while (pc < code_len) {
        int op = ops[pc];
        int arg = argvals[pc];
        pc++;
        
        switch (op) {
            case LOAD_CONST: {
                PyObject *c = PyList_GetItem(consts_list, arg);
                if (!c) goto error;
                Py_INCREF(c);
                PUSH(c);
                break;
            }
            case LOAD_VAR: {
                if (arg < 0 || arg >= nvars || !vars[arg]) {
                    PyErr_SetString(PyExc_RuntimeError, "undefined variable");
                    goto error;
                }
                Py_INCREF(vars[arg]);
                PUSH(vars[arg]);
                break;
            }
            case STORE_VAR: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = STACK_POP();
                if (arg < 0 || arg >= nvars) {
                    Py_DECREF(v);
                    PyErr_SetString(PyExc_RuntimeError, "var slot out of range");
                    goto error;
                }
                Py_XDECREF(vars[arg]);
                vars[arg] = v;
                break;
            }
            case BINARY_ADD: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = fast_long_add(a, b);
                if (!r) r = PyNumber_Add(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_SUB: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = fast_long_sub(a, b);
                if (!r) r = PyNumber_Subtract(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_MUL: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = fast_long_mul(a, b);
                if (!r) r = PyNumber_Multiply(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_DIV: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_TrueDivide(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_MOD: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_Remainder(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_AND: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_And(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_OR: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_Or(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_XOR: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_Xor(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_SHL: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_Lshift(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case BINARY_SHR: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                PyObject *r = PyNumber_Rshift(a, b);
                Py_DECREF(a); Py_DECREF(b);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case COMPARE_LT: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp;
                if (PyLong_CheckExact(a) && PyLong_CheckExact(b)) {
                    int ovf_a = 0, ovf_b = 0;
                    long long av = PyLong_AsLongLongAndOverflow(a, &ovf_a);
                    long long bv = PyLong_AsLongLongAndOverflow(b, &ovf_b);
                    if (!ovf_a && !ovf_b) {
                        cmp = (av < bv) ? 1 : 0;
                        Py_DECREF(a); Py_DECREF(b);
                        PyObject *r = cmp ? Py_True : Py_False;
                        Py_INCREF(r);
                        PUSH(r);
                        break;
                    }
                }
                cmp = PyObject_RichCompareBool(a, b, Py_LT);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case COMPARE_EQ: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp = PyObject_RichCompareBool(a, b, Py_EQ);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case COMPARE_LE: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp = PyObject_RichCompareBool(a, b, Py_LE);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case COMPARE_GT: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp = PyObject_RichCompareBool(a, b, Py_GT);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case COMPARE_GE: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp = PyObject_RichCompareBool(a, b, Py_GE);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case COMPARE_NE: {
                if (sp < 2) goto stack_underflow;
                PyObject *b = STACK_POP();
                PyObject *a = STACK_POP();
                int cmp = PyObject_RichCompareBool(a, b, Py_NE);
                Py_DECREF(a); Py_DECREF(b);
                if (cmp < 0) goto error;
                PyObject *r = cmp ? Py_True : Py_False;
                Py_INCREF(r);
                PUSH(r);
                break;
            }
            case JUMP: {
                pc = arg;
                break;
            }
            case JUMP_IF_FALSE: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = STACK_POP();
                int truthy = PyObject_IsTrue(v);
                Py_DECREF(v);
                if (truthy < 0) goto error;
                if (!truthy) pc = arg;
                break;
            }
            case JUMP_IF_TRUE: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = STACK_POP();
                int truthy = PyObject_IsTrue(v);
                Py_DECREF(v);
                if (truthy < 0) goto error;
                if (truthy) pc = arg;
                break;
            }
            case RETURN: {
                if (sp < 1) goto stack_underflow;
                result = STACK_POP();
                goto done;
            }
            case POP: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = STACK_POP();
                Py_DECREF(v);
                break;
            }
            case DUP: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = stack[sp - 1];
                Py_INCREF(v);
                PUSH(v);
                break;
            }
            case LIST_GET: {
                if (sp < 2) goto stack_underflow;
                PyObject *idx = STACK_POP();
                PyObject *lst = STACK_POP();
                PyObject *r = PyObject_GetItem(lst, idx);
                Py_DECREF(idx); Py_DECREF(lst);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            case LIST_SET: {
                if (sp < 3) goto stack_underflow;
                PyObject *val = STACK_POP();
                PyObject *idx = STACK_POP();
                PyObject *lst = STACK_POP();
                int rc = PyObject_SetItem(lst, idx, val);
                Py_DECREF(val); Py_DECREF(idx); Py_DECREF(lst);
                if (rc < 0) goto error;
                break;
            }
            case BUILD_LIST: {
                if (sp < arg) goto stack_underflow;
                PyObject *lst = PyList_New(arg);
                if (!lst) goto error;
                for (int i = arg - 1; i >= 0; i--) {
                    PyObject *v = STACK_POP();
                    PyList_SET_ITEM(lst, i, v);  /* steals ref */
                }
                PUSH(lst);
                break;
            }
            case PRINT: {
                if (sp < 1) goto stack_underflow;
                PyObject *v = STACK_POP();
                PyObject *s = PyObject_Str(v);
                Py_DECREF(v);
                if (!s) goto error;
                if (output_list && output_list != Py_None) {
                    if (PyList_Append(output_list, s) < 0) {
                        Py_DECREF(s);
                        goto error;
                    }
                }
                {
                    const char *utf8 = PyUnicode_AsUTF8(s);
                    if (utf8)
                        PySys_WriteStdout("%s\n", utf8);
                    else {
                        Py_DECREF(s);
                        goto error;
                    }
                }
                Py_DECREF(s);
                break;
            }
            case CALL: {
                int nargs = arg;
                if (sp < nargs + 1) goto stack_underflow;
                PyObject *args_tup = PyTuple_New(nargs);
                if (!args_tup) goto error;
                for (int i = nargs - 1; i >= 0; i--) {
                    PyObject *v = STACK_POP();
                    PyTuple_SET_ITEM(args_tup, i, v);  /* steals ref */
                }
                PyObject *func = STACK_POP();
                PyObject *r;
                if (resolver && resolver != Py_None) {
                    r = PyObject_CallFunctionObjArgs(resolver, func,
                                                     args_tup, NULL);
                } else {
                    r = PyObject_CallObject(func, args_tup);
                }
                Py_DECREF(func);
                Py_DECREF(args_tup);
                if (!r) goto error;
                PUSH(r);
                break;
            }
            default: {
                PyErr_Format(PyExc_RuntimeError, "unknown opcode %d", op);
                goto error;
            }
        }
        continue;
        
    stack_underflow:
        PyErr_SetString(PyExc_RuntimeError, "stack underflow");
        goto error;
    }
    
done:
    // Cleanup
    for (int i = 0; i < nvars; i++) {
        Py_XDECREF(vars[i]);
    }
    while (sp > 0) {
        Py_DECREF(STACK_POP());
    }
    free(ops);
    free(argvals);
    free(vars);
    return result;
    
error:
    for (int i = 0; i < nvars; i++) {
        Py_XDECREF(vars[i]);
    }
    while (sp > 0) {
        Py_DECREF(STACK_POP());
    }
    free(ops);
    free(argvals);
    free(vars);
    Py_XDECREF(result);
    return NULL;
}

static PyMethodDef VmMethods[] = {
    {"run", vm_run, METH_VARARGS, "Run bytecode VM"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef vmmodule = {
    PyModuleDef_HEAD_INIT,
    "misa_vm",
    "Misa bytecode VM (C dispatch loop)",
    -1,
    VmMethods
};

PyMODINIT_FUNC PyInit_misa_vm(void) {
    return PyModule_Create(&vmmodule);
}
