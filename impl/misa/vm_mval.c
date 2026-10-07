/*
 * Misa Bytecode VM — MVal* edition (Phase 2 of JIT plan).
 *
 * Same opcodes as vm.c, but values are MVal* (the unified runtime
 * representation shared with the AOT backend) instead of PyObject*.
 *
 * Python callables (BytecodeFunction, builtins) are wrapped as
 * M_TAG_PTR MVal* values; the CALL opcode hands them to a Python
 * resolver. This is a prototype: no Py_DECREF on the wrapped objects
 * (they're long-lived; the conservative GC keeps them alive).
 *
 * Compile: gcc -shared -fPIC $(python3-config --includes) \
 *   -I../aot/rt vm_mval.c ../aot/rt/misa_rt.c -o misa_vm_mval.so
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "misa_rt.h"

// Opcodes (must match bytecode.py / vm.c)
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

/* Convert a Python constant to MVal*. Returns NULL on unsupported type. */
static MVal *py_to_mval(PyObject *o) {
    if (o == Py_None) return misa_null();
    if (o == Py_True) return misa_bool(1);
    if (o == Py_False) return misa_bool(0);
    if (PyLong_Check(o)) {
        int ovf = 0;
        long long v = PyLong_AsLongLongAndOverflow(o, &ovf);
        if (!ovf) return misa_int(v);
        return NULL;
    }
    if (PyFloat_Check(o)) return misa_float(PyFloat_AsDouble(o));
    if (PyUnicode_Check(o)) {
        Py_ssize_t sz;
        const char *s = PyUnicode_AsUTF8AndSize(o, &sz);
        if (!s) return NULL;
        return misa_text(s, (size_t)sz);
    }
    if (PyList_Check(o)) {
        MVal *l = misa_list_new();
        Py_ssize_t n = PyList_Size(o);
        for (Py_ssize_t i = 0; i < n; i++) {
            MVal *e = py_to_mval(PyList_GetItem(o, i));
            if (!e) return NULL;
            misa_list_push(l, e);
        }
        return l;
    }
    /* Anything else (BytecodeFunction, builtin): wrap as M_TAG_PTR. */
    MVal *w = misa_ptr((void *)o);
    Py_INCREF(o);  /* never decref'd in prototype; GC keeps MVal alive */
    return w;
}

/* Convert MVal* back to Python for the resolver/output. */
static PyObject *mval_to_py(MVal *v) {
    if (!v) { Py_RETURN_NONE; }
    switch (misa_tag(v)) {
    case M_TAG_NULL: Py_RETURN_NONE;
    case M_TAG_BOOL:
        if (misa_bool_val(v)) Py_RETURN_TRUE; else Py_RETURN_FALSE;
    case M_TAG_INT: return PyLong_FromLongLong(misa_int_val(v));
    case M_TAG_FLOAT: return PyFloat_FromDouble(misa_float_val(v));
    case M_TAG_TEXT:
        return PyUnicode_FromStringAndSize(misa_text_data(v),
                                           misa_text_len(v));
    case M_TAG_PTR:
        /* Wrapped Python object: return it directly. */
        {
            PyObject *o = (PyObject *)misa_as_ptr(v);
            Py_INCREF(o);
            return o;
        }
    default:
        /* Opaque: return a string repr. */
        return PyUnicode_FromFormat("<MVal tag=%d>", misa_tag(v));
    }
}

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

    int *ops = (int*)malloc(code_len * sizeof(int));
    int *argvals = (int*)malloc(code_len * sizeof(int));
    if (!ops || !argvals) {
        free(ops); free(argvals);
        return PyErr_NoMemory();
    }
    for (Py_ssize_t i = 0; i < code_len; i++) {
        PyObject *instr = PyList_GetItem(code_list, i);
        ops[i] = (int)PyLong_AsLong(PyTuple_GetItem(instr, 0));
        argvals[i] = (int)PyLong_AsLong(PyTuple_GetItem(instr, 1));
        if (PyErr_Occurred()) {
            free(ops); free(argvals);
            return NULL;
        }
    }

    /* Convert constants to MVal* once. */
    Py_ssize_t nconsts = PyList_Size(consts_list);
    MVal **consts = (MVal**)malloc(nconsts * sizeof(MVal*));
    if (!consts) { free(ops); free(argvals); return PyErr_NoMemory(); }
    for (Py_ssize_t i = 0; i < nconsts; i++) {
        consts[i] = py_to_mval(PyList_GetItem(consts_list, i));
        if (!consts[i]) {
            free(ops); free(argvals); free(consts);
            PyErr_SetString(PyExc_TypeError,
                            "vm_mval: unsupported constant type");
            return NULL;
        }
    }

    MVal **vars = (MVal**)calloc(nvars, sizeof(MVal*));
    if (!vars) {
        free(ops); free(argvals); free(consts);
        return PyErr_NoMemory();
    }
    /* Args: Python objects -> MVal*. */
    Py_ssize_t nargs = PyTuple_Size(args_tuple);
    for (Py_ssize_t i = 0; i < nargs && i < nvars; i++) {
        vars[i] = py_to_mval(PyTuple_GetItem(args_tuple, i));
        if (!vars[i]) {
            free(ops); free(argvals); free(consts); free(vars);
            PyErr_SetString(PyExc_TypeError,
                            "vm_mval: unsupported arg type");
            return NULL;
        }
    }

#define STACK_SIZE 1024
    MVal *stack[STACK_SIZE];
    int sp = 0;

#define PUSH(v) do { \
        if (sp >= STACK_SIZE) { \
            PyErr_SetString(PyExc_RuntimeError, "stack overflow"); \
            goto error; \
        } \
        stack[sp++] = (v); \
    } while(0)
#define SPOP() (stack[--sp])

    MVal *result = NULL;
    Py_ssize_t pc = 0;

    while (pc < code_len) {
        int op = ops[pc];
        int arg = argvals[pc];
        pc++;

        /* After any runtime call, check for a pending Misa error. */
#define CHKERR() do { if (misa_pending()) goto misa_error; } while(0)

        switch (op) {
            case LOAD_CONST: {
                if (arg < 0 || arg >= nconsts) {
                    PyErr_SetString(PyExc_RuntimeError, "const out of range");
                    goto error;
                }
                PUSH(consts[arg]);
                break;
            }
            case LOAD_VAR: {
                if (arg < 0 || arg >= nvars || !vars[arg]) {
                    PyErr_SetString(PyExc_RuntimeError, "undefined variable");
                    goto error;
                }
                PUSH(vars[arg]);
                break;
            }
            case STORE_VAR: {
                if (sp < 1) goto stack_underflow;
                MVal *v = SPOP();
                if (arg < 0 || arg >= nvars) {
                    PyErr_SetString(PyExc_RuntimeError, "var slot out of range");
                    goto error;
                }
                vars[arg] = v;
                break;
            }
            case BINARY_ADD: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_add(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case BINARY_SUB: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_sub(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case BINARY_MUL: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_mul(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case BINARY_DIV: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_div(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case BINARY_MOD: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_mod(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_LT: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_lt(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_LE: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_le(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_GT: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_gt(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_GE: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_ge(a, b, 0, 0); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_EQ: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_eq(a, b); CHKERR();
                PUSH(r);
                break;
            }
            case COMPARE_NE: {
                if (sp < 2) goto stack_underflow;
                MVal *b = SPOP(); MVal *a = SPOP();
                MVal *r = misa_ne(a, b); CHKERR();
                PUSH(r);
                break;
            }
            case JUMP: {
                pc = arg;
                break;
            }
            case JUMP_IF_FALSE: {
                if (sp < 1) goto stack_underflow;
                MVal *v = SPOP();
                if (!misa_truthy(v)) pc = arg;
                break;
            }
            case JUMP_IF_TRUE: {
                if (sp < 1) goto stack_underflow;
                MVal *v = SPOP();
                if (misa_truthy(v)) pc = arg;
                break;
            }
            case RETURN: {
                if (sp < 1) goto stack_underflow;
                result = SPOP();
                goto done;
            }
            case POP: {
                if (sp < 1) goto stack_underflow;
                SPOP();
                break;
            }
            case DUP: {
                if (sp < 1) goto stack_underflow;
                PUSH(stack[sp - 1]);
                break;
            }
            case LIST_GET: {
                if (sp < 2) goto stack_underflow;
                MVal *idx = SPOP(); MVal *lst = SPOP();
                /* idx is MVal*; use raw if int. */
                MVal *r;
                if (misa_tag(idx) == M_TAG_INT)
                    r = misa_list_get_raw(lst, misa_int_val(idx), 0, 0);
                else
                    r = misa_index_get(lst, idx, 0, 0);
                CHKERR();
                PUSH(r);
                break;
            }
            case LIST_SET: {
                if (sp < 3) goto stack_underflow;
                MVal *val = SPOP(); MVal *idx = SPOP(); MVal *lst = SPOP();
                if (misa_tag(idx) == M_TAG_INT)
                    misa_list_set_raw(lst, misa_int_val(idx), val, 0, 0);
                else
                    misa_index_set(lst, idx, val, 0, 0);
                CHKERR();
                break;
            }
            case BUILD_LIST: {
                if (sp < arg) goto stack_underflow;
                MVal *lst = misa_list_new();
                /* items are pushed in order; pop in reverse. */
                MVal **tmp = (MVal**)malloc(arg * sizeof(MVal*));
                if (!tmp) { PyErr_NoMemory(); goto error; }
                for (int i = arg - 1; i >= 0; i--) tmp[i] = SPOP();
                for (int i = 0; i < arg; i++) misa_list_push(lst, tmp[i]);
                free(tmp);
                PUSH(lst);
                break;
            }
            case PRINT: {
                if (sp < 1) goto stack_underflow;
                MVal *v = SPOP();
                PyObject *s = mval_to_py(v);
                /* Render via runtime for fidelity. */
                if (output_list && output_list != Py_None) {
                    PyObject *t = mval_to_py(v);
                    if (t) {
                        /* Use Python str of the converted value. */
                        PyObject *u = PyObject_Str(t);
                        Py_DECREF(t);
                        if (u) {
                            PyList_Append(output_list, u);
                            Py_DECREF(u);
                        }
                    }
                    Py_XDECREF(s);
                } else {
                    Py_XDECREF(s);
                }
                {
                    /* Print via misa runtime text rendering. */
                    MVal *txt = misa_render(v);
                    if (txt && misa_tag(txt) == M_TAG_TEXT)
                        PySys_WriteStdout("%.*s\n", (int)misa_text_len(txt),
                                          misa_text_data(txt));
                    else
                        PySys_WriteStdout("<print>\n");
                }
                break;
            }
            case CALL: {
                int call_nargs = arg;
                if (sp < call_nargs + 1) goto stack_underflow;
                /* Build Python args tuple (convert MVal* -> PyObject*). */
                PyObject *args_tup = PyTuple_New(call_nargs);
                if (!args_tup) goto error;
                for (int i = call_nargs - 1; i >= 0; i--) {
                    MVal *mv = SPOP();
                    PyObject *po = mval_to_py(mv);
                    if (!po) { Py_DECREF(args_tup); goto error; }
                    PyTuple_SET_ITEM(args_tup, i, po);
                }
                MVal *fn = SPOP();
                PyObject *pyfn = mval_to_py(fn);  /* unwraps M_TAG_PTR */
                PyObject *r;
                if (resolver && resolver != Py_None) {
                    r = PyObject_CallFunctionObjArgs(resolver, pyfn,
                                                     args_tup, NULL);
                } else {
                    r = PyObject_CallObject(pyfn, args_tup);
                }
                Py_DECREF(pyfn);
                Py_DECREF(args_tup);
                if (!r) goto error;
                /* Convert result back to MVal*. */
                MVal *mr = py_to_mval(r);
                Py_DECREF(r);
                if (!mr) {
                    PyErr_SetString(PyExc_TypeError,
                                    "vm_mval: unsupported call result type");
                    goto error;
                }
                PUSH(mr);
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
    misa_error: {
        /* A misa_* runtime call raised; surface as RuntimeError with the
         * pending message. */
        PyErr_SetString(PyExc_RuntimeError, "misa runtime error");
        goto error;
    }
    }

done:
    {
        PyObject *pyres = mval_to_py(result);
        free(ops); free(argvals); free(consts); free(vars);
        return pyres;
    }

error:
    free(ops); free(argvals); free(consts); free(vars);
    return NULL;
}

static PyMethodDef VmMethods[] = {
    {"run", vm_run, METH_VARARGS, "Run bytecode VM (MVal* values)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef vmmodule = {
    PyModuleDef_HEAD_INIT,
    "misa_vm_mval",
    "Misa bytecode VM (MVal* unified representation)",
    -1,
    VmMethods
};

PyMODINIT_FUNC PyInit_misa_vm_mval(void) {
    return PyModule_Create(&vmmodule);
}

/* Stubs for emitter-generated symbols referenced by ui_text.c.
 * The VM doesn't use the UI driver; these satisfy the linker. */
int32_t misa_num_handlers = 0;
void *misa_handler_table = 0;
