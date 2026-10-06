/* Misa AOT runtime — Phase 7: cooperative task fibers.
 *
 * Design (see spec/PHASE7-CONCURRENCY.md):
 *  - One OS thread. Tasks are fibers with their own 256KB stacks,
 *    switched by a minimal per-arch context swap (no ucontext, no new
 *    dependencies). Round-robin cooperative scheduling: a task runs
 *    until it awaits, recvs on an empty channel, sleeps, or finishes.
 *  - Structured: every compiled function opens a scope on entry and
 *    closes it on every return path (see emit.py's scope hooks); a
 *    scope's still-running children are cancelled on exit. No orphans.
 *  - Errors: a throwing task captures {code, message}; `await`
 *    rethrows it in the awaiter's fiber. Pending errors are
 *    per-fiber (misa_set_pending_slot_fn), so a raise in one fiber is
 *    invisible to the others.
 *  - GC: every MVal comes from misa_alloc (Boehm when MISA_USE_GC).
 *    TCBs and channels are GC-heap objects (scanned automatically).
 *    Fiber stacks are malloc'd and registered with GC_add_roots on
 *    creation / GC_remove_roots before free, so MVal*s held only on a
 *    parked fiber's stack keep their referents alive.
 *  - HTTP: on builds with libcurl (MISA_USE_CURL) the scheduler owns a
 *    curl_multi handle; http.get from any fiber registers an easy
 *    handle and yields until completion, so fetches truly overlap.
 *    fetch_all is order-preserving parallel GET over the same handle.
 *    Without curl (Android/JNI, web/fetch, iOS) http.get keeps its
 *    existing blocking transport and fetch_all degrades to sequential.
 *  - Emscripten (MISA_NO_FIBERS): wasm cannot do this stack switch;
 *    spawn raises a clean E600 (spec's documented fallback). Channels,
 *    sleep and fetch_all still work single-threaded.
 */
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif
#include "misa_rt.h"
#include "misa_platform.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef MISA_USE_GC
#include <gc.h>
#endif
#ifdef MISA_USE_CURL
#include <curl/curl.h>
#endif
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#ifdef __EMSCRIPTEN__
/* Web: no stack switching under wasm (see file header). */
#define MISA_NO_FIBERS 1
#endif

/* ------------------------------------------------------------------ */
/* fibers                                                             */
/* ------------------------------------------------------------------ */

#ifndef MISA_NO_FIBERS

#if defined(__x86_64__)
typedef struct {
    void *sp;                          /* 0 */
    void *rbx, *rbp, *r12, *r13, *r14, *r15;  /* 8..48 */
} fiber_ctx_t;
#elif defined(__aarch64__)
typedef struct {
    void *sp;                          /* 0 */
    void *lr;                          /* 8  (x30) */
    void *x19, *x20, *x21, *x22, *x23, *x24, *x25, *x26, *x27, *x28; /* 16..88 */
    void *fp;                          /* 96 (x29) */
} fiber_ctx_t;
#elif defined(__arm__)
/* 32-bit ARM (Android armv7). The asm below is valid Thumb-2 too. */
typedef struct {
    void *sp;                          /* 0 */
    void *lr;                          /* 4 */
    void *r4, *r5, *r6, *r7, *r8, *r9, *r10, *r11; /* 8..36 */
} fiber_ctx_t;
#else
#error "fiber_swap needs a context-switch implementation for this architecture"
#endif

/* Switch from *from to *to. Naked: no prologue/epilogue, so on entry
 * rsp/sp points at the return address (x86_64) and the swap is exact.
 * Only callee-saved registers need saving; caller-saved ones are the
 * compiler's business across this (opaque) call. A fresh fiber's
 * context has its entry trampoline pre-installed (x86_64: pushed as the
 * return address; AArch64/ARM: in lr), so the first swap `ret`s into it.
 *
 * NOTE: this runs with no C prologue — the body must be pure asm and
 * must not touch the C stack. */
#if defined(__x86_64__)
__attribute__((naked)) static void fiber_swap(fiber_ctx_t *from,
                                              fiber_ctx_t *to) {
    __asm__ volatile(
        "movq %rsp, 0(%rdi)\n\t"
        "movq %rbx, 8(%rdi)\n\t"
        "movq %rbp, 16(%rdi)\n\t"
        "movq %r12, 24(%rdi)\n\t"
        "movq %r13, 32(%rdi)\n\t"
        "movq %r14, 40(%rdi)\n\t"
        "movq %r15, 48(%rdi)\n\t"
        "movq 48(%rsi), %r15\n\t"
        "movq 40(%rsi), %r14\n\t"
        "movq 32(%rsi), %r13\n\t"
        "movq 24(%rsi), %r12\n\t"
        "movq 16(%rsi), %rbp\n\t"
        "movq 8(%rsi), %rbx\n\t"
        "movq 0(%rsi), %rsp\n\t"
        "ret\n\t"
    );
}
#elif defined(__aarch64__)
__attribute__((naked)) static void fiber_swap(fiber_ctx_t *from,
                                              fiber_ctx_t *to) {
    __asm__ volatile(
        "mov x2, sp\n\t"
        "str x2, [x0, #0]\n\t"
        "str x30, [x0, #8]\n\t"
        "stp x19, x20, [x0, #16]\n\t"
        "stp x21, x22, [x0, #32]\n\t"
        "stp x23, x24, [x0, #48]\n\t"
        "stp x25, x26, [x0, #64]\n\t"
        "stp x27, x28, [x0, #80]\n\t"
        "str x29, [x0, #96]\n\t"
        "ldr x2, [x1, #0]\n\t"
        "mov sp, x2\n\t"
        "ldr x30, [x1, #8]\n\t"
        "ldp x19, x20, [x1, #16]\n\t"
        "ldp x21, x22, [x1, #32]\n\t"
        "ldp x23, x24, [x1, #48]\n\t"
        "ldp x25, x26, [x1, #64]\n\t"
        "ldp x27, x28, [x1, #80]\n\t"
        "ldr x29, [x1, #96]\n\t"
        "ret\n\t"
    );
}
#elif defined(__arm__)
__attribute__((naked)) static void fiber_swap(fiber_ctx_t *from,
                                              fiber_ctx_t *to) {
    __asm__ volatile(
        "str sp, [r0, #0]\n\t"
        "str lr, [r0, #4]\n\t"
        "str r4, [r0, #8]\n\t"
        "str r5, [r0, #12]\n\t"
        "str r6, [r0, #16]\n\t"
        "str r7, [r0, #20]\n\t"
        "str r8, [r0, #24]\n\t"
        "str r9, [r0, #28]\n\t"
        "str r10, [r0, #32]\n\t"
        "str r11, [r0, #36]\n\t"
        "ldr sp, [r1, #0]\n\t"
        "ldr lr, [r1, #4]\n\t"
        "ldr r4, [r1, #8]\n\t"
        "ldr r5, [r1, #12]\n\t"
        "ldr r6, [r1, #16]\n\t"
        "ldr r7, [r1, #20]\n\t"
        "ldr r8, [r1, #24]\n\t"
        "ldr r9, [r1, #28]\n\t"
        "ldr r10, [r1, #32]\n\t"
        "ldr r11, [r1, #36]\n\t"
        "bx lr\n\t"
    );
}
#endif

#define FIBER_STACK_SIZE (256u * 1024u)

#endif /* !MISA_NO_FIBERS */

/* ------------------------------------------------------------------ */
/* tasks and channels                                                 */
/* ------------------------------------------------------------------ */

/* Task states. */
enum { T_READY, T_RUNNING, T_WAITING, T_DONE };
/* Wait kinds (valid when state == T_WAITING). */
enum { W_NONE, W_SLEEP, W_TASK, W_CHAN, W_HTTP, W_FETCH, W_SOCK };

typedef struct MisaTask MisaTask;
typedef struct MisaChan MisaChan;
typedef struct MisaHttpReq MisaHttpReq;
typedef struct FetchBatch FetchBatch;
typedef struct SockWait SockWait;
/* Wave 3 (networking): defined in the sockets section at end of file. */
static void sock_wait_remove(SockWait *w);
static void pump_socks(void);

struct MisaChan {
    MVal *queue;          /* M_TAG_LIST of buffered values (GC-managed) */
    int64_t head;         /* pop index into queue */
    int closed;
    MisaTask *waiters;    /* tasks blocked in recv (via wait_next) */
};

struct MisaTask {
    MisaTask *all_next;   /* global task list (never unlinked) */
    MisaTask *q_next;     /* ready-queue link */
    int id;
    int state;
    int wait_kind;
    /* body */
    MisaTaskFn fn;
    MVal *env;
    int spawn_depth;      /* spawner's scope depth (for cancellation) */
    /* outcome */
    MVal *result;
    MVal *error;          /* M_TAG_ERROR when the task threw */
    int err_line, err_col;
    int awaited;          /* someone called await on this task */
    int cancelled;
    int started;          /* fiber entered at least once */
#ifndef MISA_NO_FIBERS
    fiber_ctx_t ctx;
    void *stack;          /* malloc'd; GC_add_roots'd */
#endif
    /* waiting */
    uint64_t deadline_ms; /* W_SLEEP / W_TASK-with-timeout */
    MisaTask *wait_task;  /* W_TASK */
    MisaChan *wait_chan;  /* W_CHAN */
#ifdef MISA_USE_CURL
    MisaHttpReq *http_req;/* W_HTTP */
#endif
    FetchBatch *fetch_batch; /* W_FETCH */
    SockWait *wait_sock;    /* W_SOCK */
    MisaTask *wait_next;  /* link in a waiters list */
    MisaTask *waiters;    /* tasks awaiting THIS task */
    /* structured scopes: children spawned by this task */
    MisaTask *child_head; /* via child_next */
    MisaTask *child_next;
    int scope_depth;
    /* per-fiber pending-error slot (installed as THE slot) */
    MisaPending pending;
};

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

/* The initial thread is task #0. It never has a fiber stack; the
 * scheduler swaps other fibers against its live context. */
static MisaTask main_task;
static MisaTask *misa_current;
static MisaTask *g_all;       /* all tasks, incl. main_task */
static MisaTask *g_ready_h, *g_ready_t;
static int g_next_id = 1;
static int g_init_done = 0;

static MisaPending *task_pending_slot(void) {
    return &misa_current->pending;
}

static void enqueue(MisaTask *t) {
    t->q_next = NULL;
    if (g_ready_t) g_ready_t->q_next = t;
    else g_ready_h = t;
    g_ready_t = t;
}

static MisaTask *dequeue_ready(void) {
    MisaTask *t = g_ready_h;
    if (t) {
        g_ready_h = t->q_next;
        if (!g_ready_h) g_ready_t = NULL;
        t->q_next = NULL;
    }
    return t;
}

/* Remove t from the waiters list headed at *head (no-op if absent). */
static void waiter_remove(MisaTask **head, MisaTask *t) {
    while (*head) {
        if (*head == t) {
            *head = t->wait_next;
            t->wait_next = NULL;
            return;
        }
        head = &(*head)->wait_next;
    }
}

static void waiter_add(MisaTask **head, MisaTask *t) {
    t->wait_next = *head;
    *head = t;
}

/* Wake every task awaiting t (t just finished). */
static void wake_waiters(MisaTask *t) {
    MisaTask *w = t->waiters;
    t->waiters = NULL;
    while (w) {
        MisaTask *n = w->wait_next;
        w->wait_next = NULL;
        w->wait_task = NULL;
        w->wait_kind = W_NONE;
        w->state = T_READY;
        enqueue(w);
        w = n;
    }
}

void misa_task_init(void) {
    if (g_init_done) return;
    g_init_done = 1;
    memset(&main_task, 0, sizeof main_task);
    main_task.id = 0;
    main_task.state = T_RUNNING;
    main_task.started = 1;
    misa_current = &main_task;
    g_all = &main_task;
    misa_set_pending_slot_fn(task_pending_slot);
#ifdef MISA_USE_CURL
    curl_global_init(CURL_GLOBAL_DEFAULT);
#endif
}

/* ------------------------------------------------------------------ */
/* fiber lifecycle                                                    */
/* ------------------------------------------------------------------ */

#ifndef MISA_NO_FIBERS

/* The task whose fiber is being entered for the first time. Set by
 * sched_enter immediately before the swap; read once by the
 * trampoline. Single-threaded, so no race. */
static MisaTask *fiber_entry;

static void misa_task_finish(MisaTask *t, MVal *res);

static void fiber_trampoline(void) {
    MisaTask *t = fiber_entry;
    fiber_entry = NULL;
    MVal *res = t->fn(t->env);
    misa_task_finish(t, res);
    abort(); /* unreachable: finish swaps away forever */
}

static int fiber_stack_init(MisaTask *t) {
    void *stk = malloc(FIBER_STACK_SIZE);
    if (!stk) return 0;
    t->stack = stk;
    uintptr_t top = (uintptr_t)stk + FIBER_STACK_SIZE;
#if defined(__x86_64__)
    /* The first `ret` into the fiber pops the trampoline address: for
     * the trampoline to see correct post-`call` state (rsp%16==8),
     * rsp must be 16-aligned BEFORE the pop. */
    top &= ~(uintptr_t)15;
    top -= 16;
    *(void **)top = (void *)fiber_trampoline;
    t->ctx.sp = (void *)top;
#elif defined(__aarch64__)
    top &= ~(uintptr_t)15;
    t->ctx.sp = (void *)top;
    t->ctx.lr = (void *)fiber_trampoline;
#elif defined(__arm__)
    top &= ~(uintptr_t)7;
    t->ctx.sp = (void *)top;
    t->ctx.lr = (void *)fiber_trampoline;
#endif
#ifdef MISA_USE_GC
    /* The parked fiber's C stack holds live MVal*s — the collector
     * must see them. (TCBs/channels are GC-heap objects already.) */
    GC_add_roots(stk, (char *)stk + FIBER_STACK_SIZE);
#endif
    return 1;
}

static void fiber_stack_free(MisaTask *t) {
    if (!t->stack) return;
#ifdef MISA_USE_GC
    GC_remove_roots(t->stack, (char *)t->stack + FIBER_STACK_SIZE);
#endif
    free(t->stack);
    t->stack = NULL;
}

/* Park the current fiber back on the main thread. On resume,
 * misa_current is restored to this fiber. */
static void task_yield(void) {
    MisaTask *self = misa_current;
    fiber_swap(&self->ctx, &main_task.ctx);
    misa_current = self;
}

/* Run task t on the main thread until it yields or finishes. */
static void sched_enter(MisaTask *t) {
    fiber_entry = t;
    misa_current = t;
    t->state = T_RUNNING;
    t->started = 1;
    fiber_swap(&main_task.ctx, &t->ctx);
    misa_current = &main_task;
}

/* A task body returned (or its pending slot holds a thrown error).
 * Runs on the fiber's own stack: capture the outcome, wake awaiters,
 * and swap home. The fiber never runs again; the scheduler frees its
 * stack once it regains control. */
static void misa_task_finish(MisaTask *t, MVal *res) {
    MisaPending *ps = &t->pending;
    if (ps->err) {
        t->error = ps->err;
        t->err_line = ps->line;
        t->err_col = ps->col;
        ps->err = NULL;
        ps->line = 0;
        ps->col = 0;
    } else {
        t->result = res;
    }
    t->state = T_DONE;
    wake_waiters(t);
    misa_current = &main_task;
    fiber_swap(&t->ctx, &main_task.ctx);
    abort(); /* unreachable */
}

#else /* MISA_NO_FIBERS */

static void task_yield(void) {
    misa_platform_fatal("E300", "fiber yield without fibers");
}

#endif /* MISA_NO_FIBERS */

/* Free stacks of finished tasks. Runs on the main thread, never on a
 * fiber being freed. */
static void sweep_done_stacks(void) {
#ifndef MISA_NO_FIBERS
    for (MisaTask *t = g_all; t; t = t->all_next) {
        if (t != &main_task && t->state == T_DONE && t->stack)
            fiber_stack_free(t);
    }
#endif
}

/* ------------------------------------------------------------------ */
/* scheduler                                                          */
/* ------------------------------------------------------------------ */

static void pump_net(void);      /* defined with the HTTP layer */
static void wake_sleepers(void); /* timers + await deadlines */

typedef struct {
    int (*done)(void *);  /* nonzero -> stop looping */
    void *arg;
    uint64_t deadline;    /* 0 = none; monotonic ms */
} SchedUntil;

static void idle_sleep(uint64_t ms) {
#ifdef MISA_USE_CURL
    extern void *misa_curl_multi_handle(void);
    void *mh = misa_curl_multi_handle();
    if (mh) {
        int n = 0;
        long w = ms > 1000 ? 1000 : (long)ms;
        curl_multi_wait((CURLM *)mh, NULL, 0, (int)w, &n);
        return;
    }
#endif
#ifdef __EMSCRIPTEN__
    emscripten_sleep(ms > 100 ? 100 : (unsigned)ms);
#else
    struct timespec ts;
    ts.tv_sec = (time_t)(ms / 1000);
    ts.tv_nsec = (long)((ms % 1000) * 1000000L);
    nanosleep(&ts, NULL);
#endif
}

/* How long the idle path may block: bounded by the loop deadline, the
 * earliest parked-task deadline, and curl's own timeout. */
static uint64_t compute_wait_ms(SchedUntil *u) {
    uint64_t w = 50;
    uint64_t now = now_ms();
    if (u->deadline) {
        uint64_t rem = u->deadline > now ? u->deadline - now : 0;
        if (rem < w) w = rem;
    }
    for (MisaTask *t = g_all; t; t = t->all_next) {
        if (t->state == T_WAITING &&
            (t->wait_kind == W_SLEEP || t->wait_kind == W_TASK ||
             t->wait_kind == W_SOCK) &&
            t->deadline_ms) {
            uint64_t rem = t->deadline_ms > now ? t->deadline_ms - now : 0;
            if (rem < w) w = rem;
        }
    }
#ifdef MISA_USE_CURL
    {
        extern void *misa_curl_multi_handle(void);
        void *mh = misa_curl_multi_handle();
        if (mh) {
            long cto = -1;
            if (curl_multi_timeout((CURLM *)mh, &cto) == CURLM_OK &&
                cto >= 0 && (uint64_t)cto < w)
                w = (uint64_t)cto;
        }
    }
#endif
    return w;
}

static int any_waiters(void) {
    for (MisaTask *t = g_all; t; t = t->all_next)
        if (t->state == T_WAITING) return 1;
    return 0;
}

/* The scheduler loop. Always runs on the main thread. Returns when
 * u->done fires or the deadline passes. */
static void sched_loop(SchedUntil *u) {
    for (;;) {
        if (u->done && u->done(u->arg)) return;
        pump_net();
        pump_socks();
        wake_sleepers();
        if (u->done && u->done(u->arg)) return;
        MisaTask *t = dequeue_ready();
        if (t) {
            if (t->cancelled && !t->started) {
                /* Cancelled before its first run: fail without ever
                 * running the body. The awaiter (if any) gets E306. */
                if (!t->error) {
                    t->error = misa_error_new("E306", "task cancelled");
                    t->err_line = 0;
                    t->err_col = 0;
                }
                t->state = T_DONE;
                wake_waiters(t);
            } else {
#ifndef MISA_NO_FIBERS
                sched_enter(t);
#else
                /* No fibers: only the main task exists; a queued task
                 * here is a bug. */
                misa_platform_fatal("E300", "scheduler queued a fiber");
#endif
            }
            sweep_done_stacks();
            continue;
        }
        /* Nothing runnable: check deadline before sleeping. */
        if (u->deadline && now_ms() >= u->deadline) return;
        /* Nothing runnable. If nobody can wake us either, this is a
         * deadlock (v1: hang cheaply, like Go's block on nothing);
         * shutdown's cancellation breaks it. */
        if (!any_waiters()) {
            if (u->done && u->done(u->arg)) return;
            idle_sleep(10);
            continue;
        }
        idle_sleep(compute_wait_ms(u));
    }
}

static void wake_sleepers(void) {
    uint64_t now = now_ms();
    for (MisaTask *t = g_all; t; t = t->all_next) {
        if (t->state != T_WAITING) continue;
        if (t == misa_current) continue; /* don't wake the scheduler */
        if (t->wait_kind == W_SLEEP) {
            if (now >= t->deadline_ms) {
                t->state = T_READY;
                t->wait_kind = W_NONE;
                enqueue(t);
            }
        } else if (t->wait_kind == W_SOCK && t->deadline_ms) {
            /* socket op with timeout: detach from the fd wait list and
             * wake; sock_wait_fd sees !done + deadline and raises E304. */
            if (now >= t->deadline_ms) {
                if (t->wait_sock) {
                    sock_wait_remove(t->wait_sock);
                    t->wait_sock = NULL;
                }
                t->state = T_READY;
                t->wait_kind = W_NONE;
                enqueue(t);
            }
        } else if (t->wait_kind == W_TASK && t->deadline_ms) {
            /* await-with-timeout: the waiter re-checks the deadline
             * and raises E306 itself. */
            if (now >= t->deadline_ms) {
                if (t->wait_task) waiter_remove(&t->wait_task->waiters, t);
                t->wait_task = NULL;
                t->state = T_READY;
                t->wait_kind = W_NONE;
                enqueue(t);
            }
        }
    }
}

/* ------------------------------------------------------------------ */
/* spawn / cancel / scopes                                            */
/* ------------------------------------------------------------------ */

static void task_add_child(MisaTask *self, MisaTask *t) {
    t->child_next = self->child_head;
    self->child_head = t;
}

/* Forward: HTTP layer tears down in-flight requests on cancel.
 * (curl path only; the no-curl stub was dead code.) */
#ifdef MISA_USE_CURL
static void http_req_cancel(MisaHttpReq *r);
#endif
static void fetch_batch_cancel(FetchBatch *b);

/* Detach a WAITING task from whatever it is parked on. */
static void wait_detach(MisaTask *t) {
    switch (t->wait_kind) {
    case W_TASK:
        if (t->wait_task) {
            waiter_remove(&t->wait_task->waiters, t);
            t->wait_task = NULL;
        }
        break;
    case W_CHAN:
        if (t->wait_chan) {
            waiter_remove(&t->wait_chan->waiters, t);
            t->wait_chan = NULL;
        }
        break;
    case W_HTTP:
#ifdef MISA_USE_CURL
        if (t->http_req) {
            http_req_cancel(t->http_req);
            t->http_req = NULL;
        }
#endif
        break;
    case W_FETCH:
        if (t->fetch_batch) {
            fetch_batch_cancel(t->fetch_batch);
            t->fetch_batch = NULL;
        }
        break;
    case W_SOCK:
        if (t->wait_sock) {
            sock_wait_remove(t->wait_sock);
            t->wait_sock = NULL;
        }
        break;
    default:
        break;
    }
    t->wait_kind = W_NONE;
}

/* Cancel a task: it will raise E306 "task cancelled" the next time it
 * reaches a yield point (or immediately, if it never started). */
static void task_cancel(MisaTask *t) {
    if (t == &main_task || t->state == T_DONE || t->cancelled) return;
    t->cancelled = 1;
    if (t->state == T_WAITING) {
        wait_detach(t);
        t->state = T_READY;
        enqueue(t);
    }
    /* T_READY (not started): sched_loop fails it without running.
     * T_RUNNING is impossible here — single-threaded, and the
     * canceller is not the task itself. */
}

MVal *misa_task_spawn(MisaTaskFn fn, MVal *env, int line, int col) {
#ifdef MISA_NO_FIBERS
    /* Spec fallback: the web target runs a single-task loop. */
    misa_raise("E600",
               "spawn is not supported on the web target "
               "(Emscripten fibers unavailable)", line, col);
    return NULL;
#else
    MisaTask *t = (MisaTask *)misa_alloc(sizeof *t);
    if (!t) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    memset(t, 0, sizeof *t);
    t->id = g_next_id++;
    t->state = T_READY;
    t->fn = fn;
    t->env = env ? env : misa_null();
    t->spawn_depth = misa_current->scope_depth;
    task_add_child(misa_current, t);
    t->all_next = g_all;
    g_all = t;
    if (!fiber_stack_init(t)) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    enqueue(t);    MVal *v = misa_val_new(M_TAG_TASK);
    misa_val_set_ptr(v, t);
    return v;
#endif
}

/* Every compiled function calls this on entry (see emit.py). */
void misa_scope_enter(void) {
    misa_current->scope_depth++;
}

/* ... and this on every return path. Children spawned at this depth
 * that are still alive are cancelled: no orphans, ever. */
/* Remove a task from the current task's child list without cancelling
 * it. Used when a function returns a Task: ownership transfers to the
 * caller, so scope_exit must not cancel it. No-op if not found. */
void misa_scope_detach(MVal *task) {
    if (!task) return;
    MisaTask *t = (MisaTask *)misa_val_get_ptr(task);
    MisaTask *self = misa_current;
    MisaTask **link = &self->child_head;
    while (*link) {
        if (*link == t) {
            *link = t->child_next;
            t->child_next = NULL;
            return;
        }
        link = &(*link)->child_next;
    }
}

void misa_scope_exit(void) {
    MisaTask *t = misa_current;
    int d = t->scope_depth;
    MisaTask **link = &t->child_head;
    while (*link) {
        MisaTask *c = *link;
        if (c->spawn_depth == d) {
            *link = c->child_next; /* drop from this scope's list */
            c->child_next = NULL;
            if (c->state != T_DONE)
                task_cancel(c);
        } else {
            link = &c->child_next;
        }
    }
    t->scope_depth = d - 1;
}

/* ------------------------------------------------------------------ */
/* await / sleep                                                      */
/* ------------------------------------------------------------------ */

static int cond_task_done(void *arg) {
    return ((MisaTask *)arg)->state == T_DONE;
}

/* Park the current task on t's waiter list (caller sets state/kind). */
static void park_on_task(MisaTask *self, MisaTask *t) {
    self->wait_next = t->waiters;
    t->waiters = self;
}

/* Idempotent: detach self from whatever it is parked on. */
static void unpark_self(void) {
    MisaTask *self = misa_current;
    if (self->wait_kind == W_NONE) return;
    wait_detach(self);
}

MVal *misa_task_await(MVal *tv, int line, int col) {
    if (!tv || misa_tag(tv) != M_TAG_TASK) {
        misa_raise("E200", "await expects a Task", line, col);
        return NULL;
    }
    MisaTask *t = (MisaTask *)misa_val_get_ptr(tv);
    MisaTask *self = misa_current;
    if (t == self) {
        misa_raise("E306", "task cannot await itself", line, col);
        return NULL;
    }
    t->awaited = 1;
    while (t->state != T_DONE) {
        if (self->cancelled) {
            misa_raise("E306", "task cancelled", line, col);
            return NULL;
        }
        self->state = T_WAITING;
        self->wait_kind = W_TASK;
        self->wait_task = t;
        self->deadline_ms = 0;
        park_on_task(self, t);
        if (self == &main_task) {
            SchedUntil u = { cond_task_done, t, 0 };
            sched_loop(&u);
        } else {
            task_yield();
        }
        unpark_self();
    }
    if (t->error) {
        /* Rethrow the captured error in the awaiter's fiber. */
        MisaPending *ps = &self->pending;
        if (!ps->err) {
            ps->err = t->error;
            ps->line = t->err_line;
            ps->col = t->err_col;
        }
        return NULL;
    }
    return t->result;
}

MVal *misa_task_await_timeout(MVal *tv, int64_t ms, int line, int col) {
    if (!tv || misa_tag(tv) != M_TAG_TASK) {
        misa_raise("E200", "await expects a Task", line, col);
        return NULL;
    }
    MisaTask *t = (MisaTask *)misa_val_get_ptr(tv);
    MisaTask *self = misa_current;
    if (t == self) {
        misa_raise("E306", "task cannot await itself", line, col);
        return NULL;
    }
    if (ms < 0) ms = 0;
    uint64_t deadline = now_ms() + (uint64_t)ms;
    t->awaited = 1;
    while (t->state != T_DONE) {
        if (self->cancelled) {
            misa_raise("E306", "task cancelled", line, col);
            return NULL;
        }
        self->state = T_WAITING;
        self->wait_kind = W_TASK;
        self->wait_task = t;
        self->deadline_ms = deadline;
        park_on_task(self, t);
        if (self == &main_task) {
            SchedUntil u = { cond_task_done, t, deadline };
            sched_loop(&u);
        } else {
            task_yield();
        }
        unpark_self();
        /* Check timeout AFTER giving the scheduler a chance: a task
         * that completes without yielding should succeed even with
         * timeout=0. */
        if (t->state != T_DONE && now_ms() > deadline) {
            misa_raise_fmt("E306", line, col,
                           "task timed out after %lldms",
                           (long long)ms);
            return NULL;
        }
    }
    if (t->error) {
        MisaPending *ps = &self->pending;
        if (!ps->err) {
            ps->err = t->error;
            ps->line = t->err_line;
            ps->col = t->err_col;
        }
        return NULL;
    }
    return t->result;
}

MVal *misa_task_sleep(int64_t ms, int line, int col) {
#ifdef MISA_NO_FIBERS
    /* Web: no scheduler — blocking sleep, mirroring the existing
     * emscripten_fetch poll loop style. */
    {
        uint64_t deadline = now_ms() + (uint64_t)(ms < 0 ? 0 : ms);
        while (now_ms() < deadline)
            emscripten_sleep(10);
    }
    (void)line; (void)col;
    return misa_null();
#else
    MisaTask *self = misa_current;
    if (ms < 0) ms = 0;
    uint64_t deadline = now_ms() + (uint64_t)ms;
    if (self == &main_task) {
        SchedUntil u = { NULL, NULL, deadline };
        sched_loop(&u);
    } else {
        self->state = T_WAITING;
        self->wait_kind = W_SLEEP;
        self->deadline_ms = deadline;
        task_yield();
        self->wait_kind = W_NONE;
    }
    if (self->cancelled) {
        misa_raise("E306", "task cancelled", line, col);
        return NULL;
    }
    return misa_null();
#endif
}

/* ------------------------------------------------------------------ */
/* channels (unbounded FIFO; v1)                                      */
/* ------------------------------------------------------------------ */

static MisaChan *chan_from_val(MVal *v, int line, int col) {
    if (!v || misa_tag(v) != M_TAG_CHAN) {
        misa_raise("E200", "expected a Chan", line, col);
        return NULL;
    }
    return (MisaChan *)misa_val_get_ptr(v);
}

MVal *misa_chan_new(int line, int col) {
    (void)line; (void)col;
    MisaChan *c = (MisaChan *)misa_alloc(sizeof *c);
    if (!c) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    memset(c, 0, sizeof *c);
    c->queue = misa_list_new();
    MVal *v = misa_val_new(M_TAG_CHAN);
    misa_val_set_ptr(v, c);
    return v;
}

/* Drop consumed prefix so the queue list stays small. */
static void chan_compact(MisaChan *c) {
    int64_t len = misa_list_len(c->queue);
    if (c->head > 64 && c->head * 2 >= len) {
        MVal *nq = misa_list_new();
        for (int64_t i = c->head; i < len; i++)
            misa_list_push(nq, misa_list_get(c->queue, i));
        c->queue = nq;
        c->head = 0;
    }
}

MVal *misa_chan_send(MVal *chv, MVal *v, int line, int col) {
    MisaChan *c = chan_from_val(chv, line, col);
    if (!c) return NULL;
    if (c->closed) {
        misa_raise("E306", "channel closed", line, col);
        return NULL;
    }
    misa_list_push(c->queue, v);
    /* Wake one receiver (FIFO). */
    if (c->waiters) {
        MisaTask *w = c->waiters;
        c->waiters = w->wait_next;
        w->wait_next = NULL;
        w->wait_chan = NULL;
        w->wait_kind = W_NONE;
        w->state = T_READY;
        enqueue(w);
    }
    return misa_null();
}

static int cond_chan_ready(void *arg) {
    MisaChan *c = (MisaChan *)arg;
    MisaTask *self = misa_current;
    return c->head < misa_list_len(c->queue) || c->closed ||
           self->cancelled;
}

MVal *misa_chan_recv(MVal *chv, int line, int col) {
    MisaChan *c = chan_from_val(chv, line, col);
    if (!c) return NULL;
    MisaTask *self = misa_current;
    for (;;) {
        int64_t len = misa_list_len(c->queue);
        if (c->head < len) {
            MVal *v = misa_list_get(c->queue, c->head++);
            chan_compact(c);
            return v;
        }
        if (c->closed) {
            misa_raise("E306", "channel closed", line, col);
            return NULL;
        }
        if (self->cancelled) {
            misa_raise("E306", "task cancelled", line, col);
            return NULL;
        }
        self->state = T_WAITING;
        self->wait_kind = W_CHAN;
        self->wait_chan = c;
        waiter_add(&c->waiters, self);
        if (self == &main_task) {
            SchedUntil u = { cond_chan_ready, c, 0 };
            sched_loop(&u);
        } else {
            task_yield();
        }
        unpark_self();
    }
}

MVal *misa_chan_close(MVal *chv, int line, int col) {
    MisaChan *c = chan_from_val(chv, line, col);
    if (!c) return NULL;
    if (!c->closed) {
        c->closed = 1;
        /* Wake all receivers: they drain, then see closed -> E306. */
        MisaTask *w = c->waiters;
        c->waiters = NULL;
        while (w) {
            MisaTask *n = w->wait_next;
            w->wait_next = NULL;
            w->wait_chan = NULL;
            w->wait_kind = W_NONE;
            w->state = T_READY;
            enqueue(w);
            w = n;
        }
    }
    return misa_null();
}

/* ------------------------------------------------------------------ */
/* HTTP: scheduler-owned curl_multi (MISA_USE_CURL)                   */
/* ------------------------------------------------------------------ */

#ifdef MISA_USE_CURL

struct MisaHttpReq {
    CURL *easy;
    char *url;                  /* strdup'd: curl keeps the pointer */
    char *buf; size_t len, cap;
    char errbuf[CURL_ERROR_SIZE];
    int oom;
    int done;
    int cancelled;
    CURLcode curl_rc;
    long http_code;
    MisaTask *task;             /* waiter to wake (single requests) */
    FetchBatch *batch;          /* non-NULL for fetch_all members */
    int batch_idx;
    MisaHttpReq *next;          /* in-flight list */
};

struct FetchBatch {
    int64_t n;
    int64_t remaining;
    MVal **texts;               /* n ordered slots */
    char first_err[256];        /* first completion-order failure */
    int had_error;
    int cancelled;
    int line, col;
    MisaTask *task;
};

static CURLM *g_multi;
static MisaHttpReq *g_reqs;

/* Lazy handle; visible to the scheduler's idle path (chunk above). */
void *misa_curl_multi_handle(void) {
    if (!g_multi)
        g_multi = curl_multi_init();
    return (void *)g_multi;
}

static size_t req_write(char *ptr, size_t sz, size_t nmemb, void *ud) {
    MisaHttpReq *r = (MisaHttpReq *)ud;
    size_t total = sz * nmemb;
    if (total > 0 && r->len + total + 1 > r->cap) {
        size_t ncap = r->cap ? r->cap * 2 : 4096;
        while (ncap < r->len + total + 1) ncap *= 2;
        char *nb = (char *)realloc(r->buf, ncap);
        if (!nb) {
            r->oom = 1;
            return 0; /* abort the transfer */
        }
        r->buf = nb;
        r->cap = ncap;
    }
    if (total) {
        memcpy(r->buf + r->len, ptr, total);
        r->len += total;
    }
    return total;
}

static MisaHttpReq *req_new(const char *url) {
    MisaHttpReq *rq = (MisaHttpReq *)calloc(1, sizeof *rq);
    if (!rq) return NULL;
    rq->url = strdup(url);
    if (!rq->url) {
        free(rq);
        return NULL;
    }
    rq->easy = curl_easy_init();
    if (!rq->easy) {
        free(rq->url);
        free(rq);
        return NULL;
    }
    curl_easy_setopt(rq->easy, CURLOPT_URL, rq->url);
    curl_easy_setopt(rq->easy, CURLOPT_WRITEFUNCTION, req_write);
    curl_easy_setopt(rq->easy, CURLOPT_WRITEDATA, rq);
    curl_easy_setopt(rq->easy, CURLOPT_PRIVATE, rq);
    curl_easy_setopt(rq->easy, CURLOPT_ERRORBUFFER, rq->errbuf);
    curl_easy_setopt(rq->easy, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(rq->easy, CURLOPT_TIMEOUT_MS, 30000L);
    curl_easy_setopt(rq->easy, CURLOPT_USERAGENT, "misa/0.3");
    return rq;
}

static void req_unlink(MisaHttpReq *rq) {
    MisaHttpReq **p = &g_reqs;
    while (*p) {
        if (*p == rq) {
            *p = rq->next;
            rq->next = NULL;
            return;
        }
        p = &(*p)->next;
    }
}

static void req_free(MisaHttpReq *rq) {
    if (!rq) return;
    if (rq->easy) {
        if (g_multi) curl_multi_remove_handle(g_multi, rq->easy);
        curl_easy_cleanup(rq->easy);
        rq->easy = NULL;
    }
    req_unlink(rq);
    free(rq->url);
    free(rq->buf);
    free(rq);
}

/* Cancellation path (also used by wait_detach): tear the request down
 * now; the owning task never touches it again. */
static void http_req_cancel(MisaHttpReq *rq) {
    if (!rq) return;
    rq->cancelled = 1;
    req_free(rq);
}

static void fetch_batch_cancel(FetchBatch *b) {
    if (!b || b->cancelled) return;
    b->cancelled = 1;
    MisaHttpReq *rq = g_reqs;
    while (rq) {
        MisaHttpReq *n = rq->next;
        if (rq->batch == b) req_free(rq);
        rq = n;
    }
}

static void batch_free(FetchBatch *b) {
    if (!b) return;
    free(b->texts);
    free(b);
}

/* Non-raising validation for a batch member (pump runs in the
 * scheduler; raising there would pollute the main fiber's slot). */
static void batch_note_result(FetchBatch *b, MisaHttpReq *rq) {
    if (b->cancelled || b->had_error) return;
    if (rq->oom) {
        b->had_error = 1;
        snprintf(b->first_err, sizeof b->first_err, "out of memory");
        return;
    }
    if (rq->curl_rc != CURLE_OK) {
        b->had_error = 1;
        const char *e =
            rq->errbuf[0] ? rq->errbuf : curl_easy_strerror(rq->curl_rc);
        snprintf(b->first_err, sizeof b->first_err, "%s", e);
        return;
    }
    if (rq->http_code < 200 || rq->http_code >= 300) {
        b->had_error = 1;
        snprintf(b->first_err, sizeof b->first_err,
                 "http.get %s -> HTTP %ld", rq->url, rq->http_code);
        return;
    }
    const unsigned char *d =
        (const unsigned char *)(rq->buf ? rq->buf : "");
    for (size_t i = 0; i < rq->len;) {
        unsigned char c = d[i];
        size_t w = c < 0x80 ? 1 : (c & 0xE0) == 0xC0 ? 2
                 : (c & 0xF0) == 0xE0 ? 3 : (c & 0xF8) == 0xF0 ? 4 : 0;
        if (!w || i + w > rq->len) {
            b->had_error = 1;
            snprintf(b->first_err, sizeof b->first_err,
                     "http.get %s: body is not UTF-8 text", rq->url);
            return;
        }
        i += w;
    }
    b->texts[rq->batch_idx] = misa_text(rq->buf ? rq->buf : "", rq->len);
}

static void batch_req_done(MisaHttpReq *rq) {
    FetchBatch *b = rq->batch;
    batch_note_result(b, rq);
    req_free(rq);
    if (--b->remaining == 0 && !b->cancelled) {
        MisaTask *t = b->task;
        t->state = T_READY;
        t->wait_kind = W_NONE;
        t->fetch_batch = NULL;
        enqueue(t);
    }
}

static void pump_net(void) {
    CURLM *multi = (CURLM *)misa_curl_multi_handle();
    if (!multi) return;
    int running = 0;
    curl_multi_perform(multi, &running);
    CURLMsg *m;
    int q = 0;
    while ((m = curl_multi_info_read(multi, &q))) {
        if (m->msg != CURLMSG_DONE) continue;
        CURL *e = m->easy_handle;
        MisaHttpReq *rq = NULL;
        curl_easy_getinfo(e, CURLINFO_PRIVATE, &rq);
        if (m->data.result == CURLE_OK)
            curl_easy_getinfo(e, CURLINFO_RESPONSE_CODE,
                              rq ? &rq->http_code : NULL);
        curl_multi_remove_handle(multi, e);
        curl_easy_cleanup(e);
        if (!rq) continue;
        rq->easy = NULL;
        if (rq->cancelled) {
            req_free(rq);
            continue;
        }
        rq->curl_rc = m->data.result;
        rq->done = 1;
        if (rq->batch) {
            batch_req_done(rq);
        } else if (rq->task) {
            MisaTask *t = rq->task;
            t->state = T_READY;
            t->wait_kind = W_NONE;
            /* t->http_req stays set: the woken task consumes and frees
             * the request (http_req_result). */
            enqueue(t);
        }
        /* Single requests are freed by the woken task (http_req_result
         * below); batch members are freed by batch_req_done. */
    }
}

static int cond_req_done(void *arg) {
    MisaHttpReq *rq = (MisaHttpReq *)arg;
    return rq->done || rq->cancelled;
}

static int cond_batch_done(void *arg) {
    FetchBatch *b = (FetchBatch *)arg;
    return b->remaining == 0 || b->cancelled;
}

/* Build the MVal for a completed single request; frees the request. */
static MVal *http_req_result(MisaHttpReq *rq, int line, int col) {
    MVal *out = NULL;
    if (rq->oom) {
        misa_raise("E300", "out of memory", line, col);
    } else if (rq->curl_rc != CURLE_OK) {
        const char *e =
            rq->errbuf[0] ? rq->errbuf : curl_easy_strerror(rq->curl_rc);
        misa_raise_fmt("E304", line, col, "http.get failed: %s", e);
    } else {
        out = misa_http_validate(rq->url, rq->buf, rq->len,
                                 rq->http_code, line, col);
    }
    req_free(rq);
    return out;
}

#else /* !MISA_USE_CURL */

static void pump_net(void) {}

/* Cancellation paths: no requests can exist without curl. */
static void fetch_batch_cancel(FetchBatch *b) { (void)b; }

#endif /* MISA_USE_CURL */

/* ------------------------------------------------------------------ */
/* http.get / fetch_all builtins                                      */
/* ------------------------------------------------------------------ */

MVal *misa_http_get(MVal *url, int line, int col) {
    if (!url || misa_tag(url) != M_TAG_TEXT) {
        misa_raise("E200", "http.get expects a Text URL", line, col);
        return NULL;
    }
    const char *u = misa_text_z(url);
    if (strncmp(u, "http://", 7) != 0 && strncmp(u, "https://", 8) != 0) {
        misa_raise_fmt("E304", line, col,
                       "http.get needs an http(s) URL, got '%s'", u);
        return NULL;
    }
#ifdef MISA_USE_CURL
    if (misa_curl_multi_handle()) {
        /* Fiber-aware: register on the scheduler's multi handle and
         * yield until completion — other tasks run meanwhile. */
        MisaHttpReq *rq = req_new(u);
        if (!rq) {
            misa_raise("E300", "out of memory", line, col);
            return NULL;
        }
        MisaTask *self = misa_current;
        rq->task = self;
        curl_multi_add_handle((CURLM *)misa_curl_multi_handle(),
                              rq->easy);
        rq->next = g_reqs;
        g_reqs = rq;
        self->state = T_WAITING;
        self->wait_kind = W_HTTP;
        self->http_req = rq;
        if (self == &main_task) {
            SchedUntil h = { cond_req_done, rq, 0 };
            sched_loop(&h);
        } else {
            task_yield();
        }
        self->wait_kind = W_NONE;
        rq = self->http_req;
        self->http_req = NULL;
        if (self->cancelled) {
            /* Completed (or torn down) but the task was cancelled
             * before consuming: drop the request. */
            if (rq) req_free(rq);
            misa_raise("E306", "task cancelled", line, col);
            return NULL;
        }
        return http_req_result(rq, line, col);
    }
#endif
    /* Blocking fallback (no libcurl: Android/JNI, web/fetch, iOS). */
    {
        MisaHttpResp r;
        memset(&r, 0, sizeof r);
        if (!misa_platform_http_get(u, 30.0, &r)) {
            misa_raise_fmt("E304", line, col, "http.get failed: %s",
                           r.err);
            misa_platform_http_free(&r);
            return NULL;
        }
        MVal *t = misa_http_validate(u, r.body, r.len, r.status,
                                     line, col);
        misa_platform_http_free(&r);
        return t;
    }
}

#ifdef MISA_USE_CURL
static MVal *fetch_all_async(MVal *urls, int64_t n, MVal *out,
                             int line, int col) {
    MisaTask *self = misa_current;
    FetchBatch *b = (FetchBatch *)calloc(1, sizeof *b);
    if (!b) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    b->n = n;
    b->remaining = n;
    b->task = self;
    b->line = line;
    b->col = col;
    b->texts = (MVal **)calloc((size_t)n, sizeof(MVal *));
    if (!b->texts) {
        free(b);
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    for (int64_t i = 0; i < n; i++) {
        MVal *uv = misa_list_get(urls, i);
        const char *us =
            (uv && misa_tag(uv) == M_TAG_TEXT) ? misa_text_z(uv) : NULL;
        if (!us || (strncmp(us, "http://", 7) != 0 &&
                    strncmp(us, "https://", 8) != 0)) {
            fetch_batch_cancel(b);
            batch_free(b);
            if (!us)
                misa_raise("E200",
                           "fetch_all expects a list of Text URLs",
                           line, col);
            else
                misa_raise_fmt("E304", line, col,
                               "http.get needs an http(s) URL, got '%s'",
                               us);
            return NULL;
        }
        MisaHttpReq *rq = req_new(us);
        if (!rq) {
            fetch_batch_cancel(b);
            batch_free(b);
            misa_raise("E300", "out of memory", line, col);
            return NULL;
        }
        rq->task = self;
        rq->batch = b;
        rq->batch_idx = (int)i;
        curl_multi_add_handle((CURLM *)misa_curl_multi_handle(),
                              rq->easy);
        rq->next = g_reqs;
        g_reqs = rq;
    }
    self->state = T_WAITING;
    self->wait_kind = W_FETCH;
    self->fetch_batch = b;
    if (self == &main_task) {
        SchedUntil fh = { cond_batch_done, b, 0 };
        sched_loop(&fh);
    } else {
        task_yield();
    }
    self->wait_kind = W_NONE;
    self->fetch_batch = NULL;
    if (self->cancelled || b->cancelled) {
        batch_free(b);
        misa_raise("E306", "task cancelled", line, col);
        return NULL;
    }
    if (b->had_error) {
        misa_raise_fmt("E304", b->line, b->col, "http.get failed: %s",
                       b->first_err);
        batch_free(b);
        return NULL;
    }
    for (int64_t i = 0; i < n; i++)
        misa_list_push(out, b->texts[i]);
    batch_free(b);
    return out;
}
#endif

MVal *misa_fetch_all(MVal *urls, int line, int col) {
    if (!urls || misa_tag(urls) != M_TAG_LIST) {
        misa_raise("E200", "fetch_all expects a list of URLs",
                   line, col);
        return NULL;
    }
    int64_t n = misa_list_len(urls);
    MVal *out = misa_list_new();
#ifdef MISA_USE_CURL
    if (n > 0 && misa_curl_multi_handle())
        return fetch_all_async(urls, n, out, line, col);
#endif
    /* Sequential fallback: no libcurl (Android/JNI, web/fetch, iOS),
     * or an empty list. Documented in the spec (§Backends). */
    for (int64_t i = 0; i < n; i++) {
        MVal *body = misa_http_get(misa_list_get(urls, i), line, col);
        if (!body) return NULL;
        misa_list_push(out, body);
    }
    return out;
}

/* ------------------------------------------------------------------ */
/* program end                                                        */
/* ------------------------------------------------------------------ */

static int cond_all_done(void *arg) {
    (void)arg;
    for (MisaTask *t = g_all; t; t = t->all_next)
        if (t != &main_task && t->state != T_DONE) return 0;
    return 1;
}

void misa_task_shutdown(void) {
    if (!g_init_done) return;
    /* Cancel everything outstanding, then give tasks a bounded grace
     * period to unwind to a yield point (a task stuck in a pure CPU
     * loop never yields — same documented caveat as the scheduler). */
    for (MisaTask *t = g_all; t; t = t->all_next)
        task_cancel(t);
    {
        SchedUntil u = { cond_all_done, NULL, now_ms() + 5000 };
        sched_loop(&u);
    }
    /* Force-finish stragglers: abandon their fibers. */
    for (MisaTask *t = g_all; t; t = t->all_next) {
        if (t == &main_task || t->state == T_DONE) continue;
        if (!t->error && !t->result) {
            t->error = misa_error_new("E306", "task cancelled");
            t->err_line = 0;
            t->err_col = 0;
        }
        t->state = T_DONE;
        wake_waiters(t);
    }
    sweep_done_stacks();
    /* A task never awaited still reports its error — never silent. */
    for (MisaTask *t = g_all; t; t = t->all_next) {
        if (t == &main_task || !t->error || t->awaited) continue;
        misa_report_task_error(t->error, t->id, t->err_line, t->err_col);
    }
#ifdef MISA_USE_CURL
    if (g_multi) {
        curl_multi_cleanup(g_multi);
        g_multi = NULL;
    }
#endif
}
/* ------------------------------------------------------------------ */
/* Wave 3 (networking): TCP sockets + HTTP server                      */
/* ------------------------------------------------------------------ */
/*
 * Design:
 *  - Every socket fd is non-blocking. An operation that would block
 *    parks the current task (W_SOCK) until its fd is readable/writable;
 *    the scheduler's pump_socks() wakes ready tasks via select(), so a
 *    slow peer never stalls other tasks.
 *  - socket.set_timeout becomes a task deadline: wake_sleepers wakes
 *    the task at the deadline and the op raises E304.
 *  - Structured cancellation detaches the task from its SockWait via
 *    wait_detach; the op then raises E306. No orphaned waits.
 *  - http.serve runs a sequential accept loop on raw sockets (no
 *    libmicrohttpd): parse request line + headers + Content-Length
 *    body, invoke the Misa handler closure, write a minimal response.
 *    A handler that throws is isolated per connection (-> 500).
 */
#ifdef __EMSCRIPTEN__
/* Web: no raw TCP sockets under wasm — clean E600, never a trap. */
MVal *misa_net_connect(MVal *host, MVal *port, int line, int col) {
    (void)host; (void)port;
    misa_raise("E600", "net.connect is not supported on web", line, col);
    return NULL;
}
MVal *misa_net_listen(MVal *port, int line, int col) {
    (void)port;
    misa_raise("E600", "net.listen is not supported on web", line, col);
    return NULL;
}
MVal *misa_net_accept(MVal *lis, int line, int col) {
    (void)lis;
    misa_raise("E600", "net.accept is not supported on web", line, col);
    return NULL;
}
MVal *misa_net_port(MVal *lis, int line, int col) {
    (void)lis;
    misa_raise("E600", "net.port is not supported on web", line, col);
    return NULL;
}
MVal *misa_listener_close(MVal *lis, int line, int col) {
    (void)lis;
    misa_raise("E600", "listener.close is not supported on web",
               line, col);
    return NULL;
}
MVal *misa_socket_send(MVal *sock, MVal *data, int line, int col) {
    (void)sock; (void)data;
    misa_raise("E600", "socket.send is not supported on web", line, col);
    return NULL;
}
MVal *misa_socket_recv(MVal *sock, MVal *max, int line, int col) {
    (void)sock; (void)max;
    misa_raise("E600", "socket.recv is not supported on web", line, col);
    return NULL;
}
MVal *misa_socket_close(MVal *sock, int line, int col) {
    (void)sock;
    misa_raise("E600", "socket.close is not supported on web",
               line, col);
    return NULL;
}
MVal *misa_socket_set_timeout(MVal *sock, MVal *ms, int line, int col) {
    (void)sock; (void)ms;
    misa_raise("E600", "socket.set_timeout is not supported on web",
               line, col);
    return NULL;
}
MVal *misa_http_serve(MVal *port, MVal *handler, int line, int col) {
    (void)port; (void)handler;
    misa_raise("E600", "http.serve is not supported on web", line, col);
    return NULL;
}
#else /* POSIX sockets: Linux, macOS, Android, iOS */

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <fcntl.h>
#include <errno.h>
#include <unistd.h>
#include <sys/select.h>

/* -- handles ------------------------------------------------------- */

typedef struct {
    int fd;
    int closed;
    int64_t timeout_ms; /* -1 = none (socket.set_timeout) */
} MisaSock;

static MVal *sock_wrap(int fd, uint8_t tag) {
    MisaSock *s = (MisaSock *)misa_alloc(sizeof(MisaSock));
    if (!s) return NULL;
    s->fd = fd;
    s->closed = 0;
    s->timeout_ms = -1;
    MVal *v = misa_val_new(tag);
    misa_val_set_ptr(v, s);
    return v;
}

static MisaSock *sock_unwrap(MVal *v, uint8_t tag, const char *op,
                             int line, int col) {
    const char *tname = (tag == M_TAG_SOCKET) ? "Socket" : "Listener";
    if (!v || misa_tag(v) != tag) {
        misa_raise_fmt("E200", line, col, "%s: expected a %s", op, tname);
        return NULL;
    }
    MisaSock *s = (MisaSock *)misa_val_get_ptr(v);
    if (!s || s->closed || s->fd < 0) {
        misa_raise_fmt("E304", line, col, "%s: socket is closed", op);
        return NULL;
    }
    return s;
}

static int net_set_nonblock(int fd) {
    int fl = fcntl(fd, F_GETFL, 0);
    if (fl < 0) return -1;
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

/* -- scheduler integration ------------------------------------------ */

struct SockWait {
    SockWait *next;
    int fd;
    int want_write; /* 0 = wait readable, 1 = wait writable */
    MisaTask *task;
    int done;
};

static SockWait *g_sock_waits = NULL;

/* Idempotent: unlink w from the wait list (cancellation path and the
 * woken task's own resume path both call this). */
static void sock_wait_remove(SockWait *w) {
    SockWait **pp = &g_sock_waits;
    while (*pp) {
        if (*pp == w) {
            *pp = w->next;
            return;
        }
        pp = &(*pp)->next;
    }
}

static void pump_socks(void) {
    if (!g_sock_waits) return;
    fd_set rfds, wfds;
    FD_ZERO(&rfds);
    FD_ZERO(&wfds);
    int maxfd = -1;
    for (SockWait *w = g_sock_waits; w; w = w->next) {
        if (w->done) continue;
        if (w->fd < 0 || w->fd >= FD_SETSIZE) continue;
        if (w->want_write) FD_SET(w->fd, &wfds);
        else FD_SET(w->fd, &rfds);
        if (w->fd > maxfd) maxfd = w->fd;
    }
    if (maxfd < 0) return;
    struct timeval tv = {0, 0};
    int n = select(maxfd + 1, &rfds, &wfds, NULL, &tv);
    if (n <= 0) return;
    for (SockWait *w = g_sock_waits; w; w = w->next) {
        if (w->done) continue;
        int ready = w->want_write ? FD_ISSET(w->fd, &wfds)
                                  : FD_ISSET(w->fd, &rfds);
        if (ready) {
            w->done = 1;
            MisaTask *t = w->task;
            t->state = T_READY;
            t->wait_kind = W_NONE;
            t->wait_sock = NULL;
            t->deadline_ms = 0;
            enqueue(t);
        }
    }
}

static int cond_sock_done(void *arg) {
    return ((SockWait *)arg)->done;
}

/* Park the current task until fd is ready. deadline_ms = 0 for none
 * (absolute monotonic ms otherwise). Returns 0 when the fd is ready,
 * -1 with a raised error on timeout (E304) or cancellation (E306). */
static int sock_wait_fd(int fd, int want_write, uint64_t deadline_ms,
                        const char *op, int line, int col) {
    MisaTask *self = misa_current;
#ifdef MISA_NO_FIBERS
    /* No scheduler (should not happen on POSIX, but be safe):
     * blocking select with the deadline. */
    {
        fd_set rfds, wfds;
        struct timeval tv;
        uint64_t now = now_ms();
        long wait_ms = 0;
        if (deadline_ms) {
            if (now >= deadline_ms) {
                misa_raise_fmt("E304", line, col,
                               "%s timed out", op);
                return -1;
            }
            wait_ms = (long)(deadline_ms - now);
        } else {
            wait_ms = 30000;
        }
        FD_ZERO(&rfds);
        FD_ZERO(&wfds);
        if (want_write) FD_SET(fd, &wfds);
        else FD_SET(fd, &rfds);
        tv.tv_sec = wait_ms / 1000;
        tv.tv_usec = (wait_ms % 1000) * 1000;
        int n = select(fd + 1, &rfds, &wfds, NULL, &tv);
        if (n <= 0) {
            misa_raise_fmt("E304", line, col, "%s timed out", op);
            return -1;
        }
        return 0;
    }
#else
    SockWait w;
    w.next = g_sock_waits;
    w.fd = fd;
    w.want_write = want_write;
    w.task = self;
    w.done = 0;
    g_sock_waits = &w;
    self->state = T_WAITING;
    self->wait_kind = W_SOCK;
    self->wait_sock = &w;
    self->deadline_ms = deadline_ms;
    if (self == &main_task) {
        SchedUntil u = {cond_sock_done, &w, deadline_ms};
        sched_loop(&u);
    } else {
        task_yield();
    }
    sock_wait_remove(&w);
    self->wait_kind = W_NONE;
    self->wait_sock = NULL;
    self->deadline_ms = 0;
    if (self->cancelled) {
        misa_raise("E306", "task cancelled", line, col);
        return -1;
    }
    if (deadline_ms && !w.done) {
        misa_raise_fmt("E304", line, col, "%s timed out", op);
        return -1;
    }
    return 0;
#endif
}

/* One non-blocking recv, parking on EAGAIN. Returns bytes read,
 * 0 = clean EOF, -1 = error raised. */
static ssize_t sock_recv_one(int fd, char *buf, size_t n,
                             int64_t timeout_ms, const char *op,
                             int line, int col) {
    for (;;) {
        ssize_t r = recv(fd, buf, n, 0);
        if (r >= 0) return r;
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            misa_raise_fmt("E304", line, col, "%s failed: %s", op,
                           strerror(errno));
            return -1;
        }
        uint64_t deadline =
            timeout_ms >= 0 ? now_ms() + (uint64_t)timeout_ms : 0;
        if (sock_wait_fd(fd, 0, deadline, op, line, col) < 0) return -1;
    }
}

/* Send all bytes, parking on EAGAIN. Returns 0 ok, -1 error raised. */
static int sock_send_all(int fd, const char *buf, size_t n,
                         int64_t timeout_ms, const char *op,
                         int line, int col) {
    size_t off = 0;
    while (off < n) {
        ssize_t r = send(fd, buf + off, n - off, 0);
        if (r > 0) {
            off += (size_t)r;
            continue;
        }
        if (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
            misa_raise_fmt("E304", line, col, "%s failed: %s", op,
                           strerror(errno));
            return -1;
        }
        uint64_t deadline =
            timeout_ms >= 0 ? now_ms() + (uint64_t)timeout_ms : 0;
        if (sock_wait_fd(fd, 1, deadline, op, line, col) < 0) return -1;
    }
    return 0;
}

/* -- builtins -------------------------------------------------------- */

MVal *misa_net_connect(MVal *hostv, MVal *portv, int line, int col) {
    if (!hostv || misa_tag(hostv) != M_TAG_TEXT) {
        misa_raise("E200", "net.connect(host: Text, port: Int)",
                   line, col);
        return NULL;
    }
    const char *host = misa_text_z(hostv);
    int64_t port = misa_as_int(portv);
    if (misa_pending()) return NULL;
    if (port < 0 || port > 65535) {
        misa_raise("E200", "net.connect: port must be 0..65535",
                   line, col);
        return NULL;
    }
    char portstr[16];
    snprintf(portstr, sizeof portstr, "%lld", (long long)port);
    struct addrinfo hints;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo *res = NULL;
    int rc = getaddrinfo(host, portstr, &hints, &res);
    if (rc != 0) {
        misa_raise_fmt("E304", line, col,
                       "net.connect %s:%lld failed: %s",
                       host, (long long)port, gai_strerror(rc));
        return NULL;
    }
    int fd = -1;
    int saved_errno = ECONNREFUSED;
    for (struct addrinfo *ai = res; ai; ai = ai->ai_next) {
        fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) {
            saved_errno = errno;
            continue;
        }
        net_set_nonblock(fd);
        if (connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
        if (errno == EINPROGRESS) break;
        saved_errno = errno;
        close(fd);
        fd = -1;
    }
    freeaddrinfo(res);
    if (fd < 0) {
        misa_raise_fmt("E304", line, col,
                       "net.connect %s:%lld failed: %s",
                       host, (long long)port, strerror(saved_errno));
        return NULL;
    }
    /* Wait for the connect to complete (writable = done). */
    if (sock_wait_fd(fd, 1, now_ms() + 30000, "net.connect",
                     line, col) < 0) {
        close(fd);
        return NULL;
    }
    int err = 0;
    socklen_t elen = sizeof err;
    getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &elen);
    if (err != 0) {
        close(fd);
        misa_raise_fmt("E304", line, col,
                       "net.connect %s:%lld failed: %s",
                       host, (long long)port, strerror(err));
        return NULL;
    }
    return sock_wrap(fd, M_TAG_SOCKET);
}

MVal *misa_net_listen(MVal *portv, int line, int col) {
    int64_t port = misa_as_int(portv);
    if (misa_pending()) return NULL;
    if (port < 0 || port > 65535) {
        misa_raise("E200", "net.listen: port must be 0..65535",
                   line, col);
        return NULL;
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        misa_raise_fmt("E304", line, col,
                       "net.listen(%lld) failed: %s",
                       (long long)port, strerror(errno));
        return NULL;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    net_set_nonblock(fd);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        int e = errno;
        close(fd);
        misa_raise_fmt("E304", line, col,
                       "net.listen(%lld) failed: %s",
                       (long long)port, strerror(e));
        return NULL;
    }
    if (listen(fd, 128) < 0) {
        int e = errno;
        close(fd);
        misa_raise_fmt("E304", line, col,
                       "net.listen(%lld) failed: %s",
                       (long long)port, strerror(e));
        return NULL;
    }
    return sock_wrap(fd, M_TAG_LISTENER);
}

MVal *misa_net_accept(MVal *lisv, int line, int col) {
    MisaSock *ls = sock_unwrap(lisv, M_TAG_LISTENER, "net.accept",
                               line, col);
    if (!ls) return NULL;
    for (;;) {
        int cfd = accept(ls->fd, NULL, NULL);
        if (cfd >= 0) {
            net_set_nonblock(cfd);
            return sock_wrap(cfd, M_TAG_SOCKET);
        }
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            misa_raise_fmt("E304", line, col,
                           "net.accept failed: %s", strerror(errno));
            return NULL;
        }
        if (sock_wait_fd(ls->fd, 0, 0, "net.accept",
                         line, col) < 0)
            return NULL;
    }
}

MVal *misa_net_port(MVal *lisv, int line, int col) {
    MisaSock *ls = sock_unwrap(lisv, M_TAG_LISTENER, "net.port",
                               line, col);
    if (!ls) return NULL;
    struct sockaddr_in addr;
    socklen_t alen = sizeof addr;
    if (getsockname(ls->fd, (struct sockaddr *)&addr, &alen) < 0) {
        misa_raise_fmt("E304", line, col,
                       "net.port failed: %s", strerror(errno));
        return NULL;
    }
    return misa_int((int64_t)ntohs(addr.sin_port));
}

MVal *misa_listener_close(MVal *lisv, int line, int col) {
    MisaSock *ls = sock_unwrap(lisv, M_TAG_LISTENER, "listener.close",
                               line, col);
    if (!ls) return NULL;
    ls->closed = 1;
    close(ls->fd);
    ls->fd = -1;
    return misa_null();
}

MVal *misa_socket_send(MVal *sockv, MVal *datav, int line, int col) {
    MisaSock *s = sock_unwrap(sockv, M_TAG_SOCKET, "socket.send",
                              line, col);
    if (!s) return NULL;
    if (!datav || misa_tag(datav) != M_TAG_TEXT) {
        misa_raise("E200", "socket.send(sock, data: Text)",
                   line, col);
        return NULL;
    }
    const char *data = misa_text_z(datav);
    size_t len = misa_text_len(datav);
    if (sock_send_all(s->fd, data, len, s->timeout_ms, "socket.send",
                      line, col) < 0)
        return NULL;
    return misa_int((int64_t)len);
}

MVal *misa_socket_recv(MVal *sockv, MVal *maxv, int line, int col) {
    MisaSock *s = sock_unwrap(sockv, M_TAG_SOCKET, "socket.recv",
                              line, col);
    if (!s) return NULL;
    int64_t max = misa_as_int(maxv);
    if (misa_pending()) return NULL;
    if (max <= 0) {
        misa_raise("E200", "socket.recv(sock, max: Int > 0)",
                   line, col);
        return NULL;
    }
    size_t n = (size_t)(max > 65536 ? 65536 : max);
    char *buf = (char *)misa_alloc(n + 1);
    if (!buf) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    ssize_t r = sock_recv_one(s->fd, buf, n, s->timeout_ms,
                              "socket.recv", line, col);
    if (r < 0) return NULL;
    /* Clean EOF -> empty Text (not an error). A multi-byte UTF-8 char
     * split across recvs is returned as raw bytes; concatenation
     * restores it (Misa Text is byte-based). */
    return misa_text(buf, (size_t)r);
}

MVal *misa_socket_close(MVal *sockv, int line, int col) {
    MisaSock *s = sock_unwrap(sockv, M_TAG_SOCKET, "socket.close",
                              line, col);
    if (!s) return NULL;
    s->closed = 1;
    close(s->fd);
    s->fd = -1;
    return misa_null();
}

MVal *misa_socket_set_timeout(MVal *sockv, MVal *msv, int line, int col) {
    MisaSock *s = sock_unwrap(sockv, M_TAG_SOCKET,
                              "socket.set_timeout", line, col);
    if (!s) return NULL;
    int64_t ms = misa_as_int(msv);
    if (misa_pending()) return NULL;
    s->timeout_ms = ms; /* <0 = no timeout */
    return misa_null();
}

/* -- HTTP server ------------------------------------------------------ */
/* Minimal HTTP/1.1 on raw sockets. Handles: request line, headers,
 * Content-Length bodies. Rejects: chunked bodies (501), oversized
 * headers (431), oversized bodies (413), malformed input (400).
 * Limits mirror the interpreter (spec/STDLIB.md documents them). */

#define HTTP_MAX_HEADERS (64 * 1024)
#define HTTP_MAX_BODY (10 * 1024 * 1024)
#define HTTP_MAX_HEADER_COUNT 100

static const char *http_reason(int status) {
    switch (status) {
    case 200: return "OK";
    case 201: return "Created";
    case 204: return "No Content";
    case 400: return "Bad Request";
    case 404: return "Not Found";
    case 405: return "Method Not Allowed";
    case 413: return "Content Too Large";
    case 431: return "Request Header Fields Too Large";
    case 500: return "Internal Server Error";
    case 501: return "Not Implemented";
    default: return "OK";
    }
}

/* Percent-decode (also '+' -> ' '). Returns a fresh Text MVal. */
static MVal *http_urldecode(const char *s, size_t n) {
    char *out = (char *)misa_alloc(n + 1);
    if (!out) return NULL;
    size_t o = 0;
    for (size_t i = 0; i < n;) {
        if (s[i] == '+') {
            out[o++] = ' ';
            i++;
        } else if (s[i] == '%' && i + 2 < n &&
                   ((s[i + 1] >= '0' && s[i + 1] <= '9') ||
                    (s[i + 1] >= 'a' && s[i + 1] <= 'f') ||
                    (s[i + 1] >= 'A' && s[i + 1] <= 'F')) &&
                   ((s[i + 2] >= '0' && s[i + 2] <= '9') ||
                    (s[i + 2] >= 'a' && s[i + 2] <= 'f') ||
                    (s[i + 2] >= 'A' && s[i + 2] <= 'F'))) {
            char hex[3] = {s[i + 1], s[i + 2], 0};
            out[o++] = (char)strtol(hex, NULL, 16);
            i += 3;
        } else {
            out[o++] = s[i++];
        }
    }
    MVal *t = misa_text(out, o);
    return t;
}

/* Parse "a=1&b=2" into a fresh Map MVal. */
static MVal *http_parse_query(const char *qs, size_t n) {
    MVal *m = misa_map_new();
    size_t i = 0;
    while (i < n) {
        size_t j = i;
        while (j < n && qs[j] != '&') j++;
        const char *part = qs + i;
        size_t plen = j - i;
        const char *eq = memchr(part, '=', plen);
        MVal *k, *v;
        if (eq) {
            k = http_urldecode(part, (size_t)(eq - part));
            v = http_urldecode(eq + 1,
                               plen - (size_t)(eq - part) - 1);
        } else {
            k = http_urldecode(part, plen);
            v = misa_text_cstr("");
        }
        if (k && v) misa_index_set(m, k, v, 0, 0);
        i = j + 1;
    }
    return m;
}

/* Read from fd until "\r\n\r\n" is seen. Returns a malloc'd buffer
 * (use misa_free), *head_len = bytes through the blank line,
 * *total = bytes read. NULL = error raised. */
static char *http_read_headers(int fd, int64_t timeout_ms,
                               size_t *head_len, size_t *total,
                               const char *op, int line, int col) {
    size_t cap = 8192, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) {
        misa_raise("E300", "out of memory", line, col);
        return NULL;
    }
    for (;;) {
        if (len + 4096 + 4 > cap) {
            cap *= 2;
            char *nb = (char *)realloc(buf, cap);
            if (!nb) {
                free(buf);
                misa_raise("E300", "out of memory", line, col);
                return NULL;
            }
            buf = nb;
        }
        ssize_t r = sock_recv_one(fd, buf + len, 4096, timeout_ms,
                                  op, line, col);
        if (r < 0) {
            free(buf);
            return NULL;
        }
        if (r == 0) {
            free(buf); /* EOF mid-headers */
            return NULL;
        }
        len += (size_t)r;
        if (len > HTTP_MAX_HEADERS) {
            free(buf);
            return NULL; /* caller maps to 431 */
        }
        /* scan only the new tail for \r\n\r\n */
        size_t scan_from = len > (size_t)r + 3 ? len - (size_t)r - 3 : 0;
        for (size_t i = scan_from; i + 3 < len; i++) {
            if (buf[i] == '\r' && buf[i + 1] == '\n' &&
                buf[i + 2] == '\r' && buf[i + 3] == '\n') {
                *head_len = i + 4;
                *total = len;
                buf[len] = 0;
                return buf;
            }
        }
    }
}

/* Invoke a Misa closure MVal (record {__closure_fn, __closure_env})
 * with a one-element args list. Returns the result, or NULL with a
 * pending error when the handler threw. */
typedef MVal *(*MisaClosureFn)(MVal *env, MVal *args);

static MVal *closure_invoke(MVal *clos, MVal *args,
                            int line, int col) {
    MVal *fnv = misa_record_get_opt(clos, "__closure_fn");
    MVal *env = misa_record_get_opt(clos, "__closure_env");
    if (!fnv || !env || misa_tag(fnv) != M_TAG_INT) {
        misa_raise("E200",
                   "http.serve: handler must be a lambda", line, col);
        return NULL;
    }
    int64_t fni = misa_as_int(fnv);
    if (misa_pending()) return NULL;
    MisaClosureFn fn = (MisaClosureFn)(void *)(intptr_t)fni;
    return fn(env, args);
}

/* Map lookup by C string key (case-sensitive; header names are
 * already lowercased by the parser). */
static MVal *map_get_cstr(MVal *m, const char *k) {
    if (!m || misa_tag(m) != M_TAG_MAP) return NULL;
    int64_t n = misa_map_len(m);
    for (int64_t i = 0; i < n; i++) {
        MVal *key = misa_map_key(m, i);
        if (key && misa_tag(key) == M_TAG_TEXT &&
            strcmp(misa_text_z(key), k) == 0)
            return misa_map_val(m, i);
    }
    return NULL;
}

/* Serve one connection: read request, invoke handler, write response.
 * Never raises for per-connection failures (they become 4xx/5xx);
 * raises only when the socket itself fails (E304) or the task is
 * cancelled (E306). */
static int http_serve_one(int fd, MVal *handler, int line, int col) {
    size_t head_len = 0, total = 0;
    char *buf = http_read_headers(fd, -1, &head_len, &total,
                                  "http.serve", line, col);
    int err_status = 0;
    if (!buf) {
        if (misa_pending()) return -1; /* real socket error */
        err_status = 431;              /* oversize headers */
    }
    MVal *req = NULL;
    size_t body_off = 0, body_have = 0;
    if (!err_status) {
        /* --- request line --- */
        char *eol = memchr(buf, '\r', head_len);
        if (!eol) err_status = 400;
        else {
            *eol = 0;
            char *sp1 = strchr(buf, ' ');
            char *sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
            char *sp3 = sp2 ? strchr(sp2 + 1, ' ') : NULL;
            if (!sp1 || !sp2 || sp3 ||
                strncmp(sp2 + 1, "HTTP/", 5) != 0) {
                err_status = 400;
            } else {
                *sp1 = 0;
                *sp2 = 0;
                const char *method = buf;
                const char *target = sp1 + 1;
                int ok = *method != 0;
                for (const char *p = method; ok && *p; p++)
                    if (!(*p >= 'A' && *p <= 'Z') && *p != '-')
                        ok = 0;
                if (!ok) err_status = 400;
                else {
                    /* --- headers --- */
                    MVal *hmap = misa_map_new();
                    int hcount = 0;
                    char *lp = eol + 2;
                    char *hend = buf + head_len - 4;
                    while (!err_status && lp < hend) {
                        char *le = memchr(lp, '\r',
                                          (size_t)(hend - lp));
                        if (!le) {
                            err_status = 400;
                            break;
                        }
                        *le = 0;
                        char *colon = strchr(lp, ':');
                        if (!colon || ++hcount > HTTP_MAX_HEADER_COUNT) {
                            err_status =
                                colon ? 431 : 400;
                            break;
                        }
                        *colon = 0;
                        /* trim + lowercase the name */
                        char *nb = lp;
                        while (*nb == ' ' || *nb == '\t') nb++;
                        char *ne = colon - 1;
                        while (ne > nb &&
                               (*ne == ' ' || *ne == '\t')) {
                            *ne = 0;
                            ne--;
                        }
                        if (!*nb) {
                            err_status = 400;
                            break;
                        }
                        for (char *p = nb; *p; p++)
                            if (*p >= 'A' && *p <= 'Z')
                                *p += 32;
                        char *vb = colon + 1;
                        while (*vb == ' ' || *vb == '\t') vb++;
                        char *ve = le - 1;
                        while (ve > vb &&
                               (*ve == ' ' || *ve == '\t')) {
                            *ve = 0;
                            ve--;
                        }
                        misa_index_set(
                            hmap, misa_text_cstr(nb),
                            misa_text_cstr(vb), 0, 0);
                        lp = le + 2;
                    }
                    if (!err_status) {
                        /* chunked? (v1: reject) */
                        MVal *te =
                            map_get_cstr(hmap,
                                         "transfer-encoding");
                        if (te && misa_tag(te) == M_TAG_TEXT &&
                            strstr(misa_text_z(te), "chunked")) {
                            err_status = 501;
                        } else {
                            /* content-length */
                            MVal *clv = map_get_cstr(
                                hmap, "content-length");
                            long body_len = 0;
                            if (clv &&
                                misa_tag(clv) == M_TAG_TEXT) {
                                char *endp = NULL;
                                body_len = strtol(
                                    misa_text_z(clv), &endp,
                                    10);
                                if (!endp ||
                                    *endp != 0 ||
                                    body_len < 0 ||
                                    body_len >
                                        HTTP_MAX_BODY) {
                                    err_status =
                                        body_len >
                                                HTTP_MAX_BODY
                                            ? 413
                                            : 400;
                                }
                            }
                            if (!err_status) {
                                body_off = head_len;
                                body_have = total - head_len;
                                /* read the rest of the body */
                                while (body_have <
                                       (size_t)body_len) {
                                    size_t want =
                                        (size_t)body_len -
                                        body_have;
                                    if (want > 65536)
                                        want = 65536;
                                    size_t cap = total +
                                                 want + 1;
                                    char *nb = (char *)realloc(
                                        buf, cap);
                                    if (!nb) {
                                        misa_raise(
                                            "E300",
                                            "out of memory",
                                            line, col);
                                        free(buf);
                                        return -1;
                                    }
                                    buf = nb;
                                    ssize_t r = sock_recv_one(
                                        fd, buf + total,
                                        want, -1,
                                        "http.serve",
                                        line, col);
                                    if (r < 0) {
                                        free(buf);
                                        return -1;
                                    }
                                    if (r == 0) {
                                        err_status = 400;
                                        break;
                                    }
                                    total += (size_t)r;
                                    body_have += (size_t)r;
                                }
                            }
                            if (!err_status) {
                                /* split target into path + query */
                                const char *qm = strchr(
                                    target, '?');
                                MVal *qmap;
                                const char *path = target;
                                size_t pathn;
                                if (qm) {
                                    pathn =
                                        (size_t)(qm -
                                                 target);
                                    qmap =
                                        http_parse_query(
                                            qm + 1,
                                            strlen(qm + 1));
                                } else {
                                    pathn = strlen(target);
                                    qmap = misa_map_new();
                                }
                                req = misa_record_new();
                                misa_record_set_cstr(
                                    req, "method",
                                    misa_text_cstr(method));
                                misa_record_set_cstr(
                                    req, "path",
                                    http_urldecode(
                                        path, pathn));
                                misa_record_set_cstr(
                                    req, "query", qmap);
                                misa_record_set_cstr(
                                    req, "headers", hmap);
                                misa_record_set_cstr(
                                    req, "body",
                                    misa_text(
                                        buf + body_off,
                                        (size_t)body_len));
                            }
                        }
                    }
                }
            }
        }
    }
    /* --- invoke handler / build response --- */
    MVal *resp = NULL;
    if (!err_status && req) {
        MVal *args = misa_list_new();
        misa_list_push(args, req);
        resp = closure_invoke(handler, args, line, col);
        if (!resp) {
            /* handler threw: per-connection isolation -> 500,
             * never kills the server. */
            misa_take_pending();
            err_status = 500;
        }
    }
    int status = err_status ? err_status : 200;
    MVal *bodyv = NULL;
    MVal *rheaders = NULL;
    if (!err_status && resp) {
        if (misa_tag(resp) != M_TAG_RECORD) {
            status = 500;
        } else {
            MVal *sv = misa_record_get_opt(resp, "status");
            if (sv) {
                if (misa_tag(sv) == M_TAG_INT)
                    status = (int)misa_as_int(sv);
                else
                    status = 500;
            }
            bodyv = misa_record_get_opt(resp, "body");
            rheaders = misa_record_get_opt(resp, "headers");
            if (rheaders &&
                misa_tag(rheaders) != M_TAG_MAP)
                rheaders = NULL;
        }
    }
    MVal *bodyt =
        (bodyv && misa_tag(bodyv) == M_TAG_TEXT)
            ? bodyv
            : (bodyv ? misa_render(bodyv) : misa_text_cstr(""));
    const char *body_bytes = misa_text_z(bodyt);
    size_t body_n = misa_text_len(bodyt);
    /* --- write response --- */
    char hbuf[1024];
    int hn = snprintf(hbuf, sizeof hbuf,
                      "HTTP/1.1 %d %s\r\n", status,
                      http_reason(status));
    int has_ctype = 0;
    /* collect user headers first (to detect content-type) */
    char uhdr[8192];
    size_t uhdr_n = 0;
    if (rheaders) {
        int64_t n = misa_map_len(rheaders);
        for (int64_t i = 0; i < n && uhdr_n < sizeof(uhdr) - 128;
             i++) {
            MVal *k = misa_map_key(rheaders, i);
            MVal *v = misa_map_val(rheaders, i);
            if (!k || !v || misa_tag(k) != M_TAG_TEXT ||
                misa_tag(v) != M_TAG_TEXT)
                continue;
            const char *ks = misa_text_z(k);
            const char *vs = misa_text_z(v);
            if (strcasecmp(ks, "content-type") == 0)
                has_ctype = 1;
            uhdr_n += (size_t)snprintf(
                uhdr + uhdr_n, sizeof(uhdr) - uhdr_n,
                "%s: %s\r\n", ks, vs);
        }
    }
    size_t hlen = (size_t)hn;
    if (!has_ctype) {
        const char *ct =
            "Content-Type: text/plain; charset=utf-8\r\n";
        size_t ctn = strlen(ct);
        if (hlen + ctn < sizeof(hbuf)) {
            memcpy(hbuf + hlen, ct, ctn);
            hlen += ctn;
        }
    }
    if (uhdr_n && hlen + uhdr_n < sizeof(hbuf)) {
        memcpy(hbuf + hlen, uhdr, uhdr_n);
        hlen += uhdr_n;
    }
    hn = snprintf(hbuf + hlen, sizeof(hbuf) - hlen,
                  "Content-Length: %zu\r\n"
                  "Connection: close\r\n\r\n",
                  body_n);
    hlen += (size_t)hn;
    int rc = 0;
    if (sock_send_all(fd, hbuf, hlen, -1, "http.serve", line,
                      col) < 0)
        rc = -1;
    else if (body_n > 0 &&
             sock_send_all(fd, body_bytes, body_n, -1,
                           "http.serve", line, col) < 0)
        rc = -1;
    free(buf);
    return rc;
}

MVal *misa_http_serve(MVal *portv, MVal *handlerv, int line, int col) {
    /* handler must be a closure record (checked again per call site
     * by closure_invoke; validate early for a clean error). */
    if (!handlerv || misa_tag(handlerv) != M_TAG_RECORD ||
        !misa_record_get_opt(handlerv, "__closure_fn")) {
        misa_raise("E200",
                   "http.serve(port, handler): handler must be a "
                   "lambda `|req: Request| { ... }`",
                   line, col);
        return NULL;
    }
    int64_t port = misa_as_int(portv);
    if (misa_pending()) return NULL;
    MVal *lisv = misa_net_listen(misa_int(port), line, col);
    if (!lisv) return NULL;
    MisaSock *ls = (MisaSock *)misa_val_get_ptr(lisv);
    MisaTask *self = misa_current;
    for (;;) {
        if (self->cancelled) {
            ls->closed = 1;
            close(ls->fd);
            ls->fd = -1;
            misa_raise("E306", "task cancelled", line, col);
            return NULL;
        }
        MVal *connv = misa_net_accept(lisv, line, col);
        if (!connv) {
            /* accept raises E304/E306; stop the server. */
            ls->closed = 1;
            close(ls->fd);
            ls->fd = -1;
            return NULL;
        }
        MisaSock *cs = (MisaSock *)misa_val_get_ptr(connv);
        int fd = cs->fd;
        cs->closed = 1; /* we own the fd now */
        cs->fd = -1;
        if (http_serve_one(fd, handlerv, line, col) < 0) {
            /* socket-level failure or cancellation: stop serving. */
            close(fd);
            ls->closed = 1;
            close(ls->fd);
            ls->fd = -1;
            return NULL; /* error already raised */
        }
        close(fd);
    }
}

#endif /* __EMSCRIPTEN__ */
