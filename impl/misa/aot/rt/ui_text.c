/* Misa AOT runtime — text UI driver (Phase 4, M2).
 *
 * The default driver for headless targets (server/desktop CLI). It
 * consumes the screen call stream through the misa_ui.h interface and
 * reproduces the v0.3 stdout tree byte-for-byte:
 *
 *   --- screen Name ---
 *   Comp(arg=val, onTap=<handler>)
 *     Child
 *
 * The 7 misa_screen_* functions (still emitted by the AOT backend and
 * still declared in misa_rt.h) are thin shims over a process-global
 * driver instance. The incremental printing behavior is preserved
 * exactly: "(" is printed eagerly when the emitter declares args
 * (via a shim-only hint — see misa_ui_text_hint_args), ")" + newline
 * when the head closes, including the partial "Name(" line when an
 * argument expression raises mid-stream and the head is never closed.
 *
 * This driver is the executable reference every future driver
 * (ui_dom.c, ui_android.c, ui_uikit.c) is checked against.
 */
#include "misa_ui.h"

#include "misa_platform.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Process-global driver instance (this driver is selected at link
 * time; generated code reaches it via misa_ui_active()). */
static MisaUI *g_ui = NULL;

/* ------------------------------------------------------------------ */
/* driver state                                                       */
/* ------------------------------------------------------------------ */

typedef struct {
    int      head_open;   /* head line printed but not yet newline-terminated */
    int      paren_open;  /* "(" printed; ")" still pending */
    int      arg_started;  /* at least one arg framed (drives ", ") */
    int32_t  bound_slot;   /* id: registry slot, -1 = unbound */
} TextWidget;

struct MisaUI {
    int         depth;
    TextWidget *widgets;    /* 1-based: widgets[id - 1] */
    int32_t     nw, wcap;
    /* shim call-stack (misa_screen_* layer) */
    MisaWidget *stack;
    int32_t     ssp, scap;
};

static void ui_grow_widgets(MisaUI *ui) {
    if (ui->nw < ui->wcap) return;
    int32_t ncap = ui->wcap ? ui->wcap * 2 : 16;
    TextWidget *nw = (TextWidget *)realloc(ui->widgets,
                                          sizeof(TextWidget) * (size_t)ncap);
    if (!nw) misa_platform_fatal("E300", "out of memory");
    ui->widgets = nw;
    ui->wcap = ncap;
}

static MisaWidget ui_new_widget(MisaUI *ui) {
    ui_grow_widgets(ui);
    TextWidget *w = &ui->widgets[ui->nw++];
    w->head_open = 0;
    w->paren_open = 0;
    w->arg_started = 0;
    w->bound_slot = -1;
    return ui->nw; /* 1-based id */
}

static TextWidget *ui_widget(MisaUI *ui, MisaWidget w) {
    if (w <= 0 || w > ui->nw) return NULL;
    return &ui->widgets[w - 1];
}

MisaUI *misa_ui_create(void) {
    MisaUI *ui = (MisaUI *)calloc(1, sizeof(MisaUI));
    if (!ui) misa_platform_fatal("E300", "out of memory");
    return ui;
}

void misa_ui_destroy(MisaUI *ui) {
    if (!ui) return;
    free(ui->widgets);
    free(ui->stack);
    free(ui);
}

/* Phase 4 (M4): the driver instance for generated code. */
MisaUI *misa_ui_active(void) {
    if (!g_ui) g_ui = misa_ui_create();
    return g_ui;
}

/* ------------------------------------------------------------------ */
/* tree construction                                                  */
/* ------------------------------------------------------------------ */

static void ui_log(MisaUI *ui, const char *s) {
    (void)ui;
    misa_platform_log(s, strlen(s));
}

static void ui_indent(MisaUI *ui) {
    for (int i = 0; i < ui->depth; i++)
        misa_platform_log("  ", 2);
}

MisaWidget misa_ui_begin_screen(MisaUI *ui, const char *name) {
    char head[1024];
    snprintf(head, sizeof head, "--- screen %s ---\n", name);
    ui_log(ui, head);
    ui->depth = 0;
    ui->nw = 0; /* fresh widget namespace per screen */
    return ui_new_widget(ui);
}

/* Close a pending head line: ")\n" when "(" was printed, else "\n". */
static void ui_close_head(MisaUI *ui, TextWidget *tw) {
    if (!tw->head_open) return;
    ui_log(ui, tw->paren_open ? ")\n" : "\n");
    tw->head_open = 0;
    tw->paren_open = 0;
    tw->arg_started = 0;
}

MisaWidget misa_ui_begin_comp(MisaUI *ui, MisaWidget parent,
                              const char *kind) {
    /* A child's arrival ends the parent's head line. (The old
     * misa_screen_comp_head_end did this eagerly; the interface closes
     * it lazily — byte-identical either way.) */
    TextWidget *pw = ui_widget(ui, parent);
    if (pw) ui_close_head(ui, pw);
    ui_indent(ui);
    ui_log(ui, kind);
    ui->depth++;
    MisaWidget w = ui_new_widget(ui);
    ui_widget(ui, w)->head_open = 1;
    return w;
}

/* Shim-only hint (not part of misa_ui.h): the emitter knows at
 * begin_comp time whether args follow (has_args). Printing "("
 * eagerly on the hint reproduces the v0.3 incremental bytes exactly —
 * including the partial "Name(" line when an argument expression
 * raises mid-stream and the head is never closed. */
void misa_ui_text_hint_args(MisaUI *ui, MisaWidget w, int has_args) {
    TextWidget *tw = ui_widget(ui, w);
    if (tw && tw->head_open && !tw->paren_open && has_args) {
        ui_log(ui, "(");
        tw->paren_open = 1;
    }
}

/* Frame one argument: "(name=" for the first, ", name=" after.
 * ("(" comes from the hint or, defensively, from here.) */
static void ui_arg_head(MisaUI *ui, MisaWidget w, const char *arg) {
    TextWidget *tw = ui_widget(ui, w);
    if (!tw) return;
    if (!tw->paren_open) {
        ui_log(ui, "(");
        tw->paren_open = 1;
    } else if (tw->arg_started) {
        ui_log(ui, ", ");
    }
    ui_log(ui, arg);
    ui_log(ui, "=");
    tw->arg_started = 1;
}

void misa_ui_set_arg(MisaUI *ui, MisaWidget w, const char *arg, MVal *v) {
    ui_arg_head(ui, w, arg);
    MVal *t = misa_render(v);
    const char *z = misa_text_z(t);
    misa_platform_log(z, misa_text_len(t));
}

void misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                         const char *event, int32_t handler_id) {
    (void)handler_id; /* the text driver never dispatches; it prints */
    ui_arg_head(ui, w, event);
    ui_log(ui, "<handler>");
}

void misa_ui_end_comp(MisaUI *ui, MisaWidget w) {
    TextWidget *tw = ui_widget(ui, w);
    if (tw) ui_close_head(ui, tw);
    if (ui->depth > 0) ui->depth--;
    misa_platform_flush();
}

void misa_ui_bind_id(MisaUI *ui, MisaWidget w, int32_t slot) {
    TextWidget *tw = ui_widget(ui, w);
    if (tw) tw->bound_slot = slot;
}

void misa_ui_show_screen(MisaUI *ui, MisaWidget root) {
    (void)root;
    misa_platform_flush();
}

/* ------------------------------------------------------------------ */
/* readback + events                                                  */
/* ------------------------------------------------------------------ */

MisaWidget misa_ui_find_bound(MisaUI *ui, int32_t slot) {
    for (int32_t i = 0; i < ui->nw; i++) {
        if (ui->widgets[i].bound_slot == slot)
            return i + 1;
    }
    return 0;
}

/* The text driver has no live widgets; TextInput readback is only
 * meaningful against real drivers (M4 compiles it there). */
MVal *misa_ui_get_text(MisaUI *ui, MisaWidget w) {
    (void)ui; (void)w;
    return NULL;
}

/* Phase 4 (M4): dispatch a compiled event handler through the
 * generated misa_handler_table[]. Returns 0 when the handler ran
 * clean, 1 when it raised (the error is reported via
 * misa_ui_report_error and never kills the process), -1 for an
 * unknown handler id. */
int32_t misa_dispatch_handler(int32_t handler_id, MVal *event) {
    if (handler_id < 0 || handler_id >= misa_num_handlers)
        return -1;
    MVal *r = misa_handler_table[handler_id](event);
    (void)r; /* handler return values are ignored */
    if (misa_pending()) {
        MVal *err = misa_take_pending();
        misa_ui_report_error(misa_ui_active(), err);
        return 1;
    }
    return 0;
}

/* Handler-boundary errors (M4): surface on the error stream in the
 * same shape as an uncaught error. Never kills the process. */
void misa_ui_report_error(MisaUI *ui, MVal *err) {
    (void)ui;
    MVal *rec = misa_catch_record(err);
    const char *code = "?";
    const char *msg = "?";
    if (rec) {
        MVal *c = misa_record_get(rec, "code", 0, 0);
        MVal *m = misa_record_get(rec, "message", 0, 0);
        if (c && !misa_pending()) code = misa_text_z(c);
        if (m && !misa_pending()) msg = misa_text_z(m);
    }
    char buf[1024];
    snprintf(buf, sizeof buf, "<unknown>: %s: %s\n", code, msg);
    misa_platform_log_err(buf, strlen(buf));
    misa_platform_flush();
}

/* ------------------------------------------------------------------ */
/* misa_screen_* compatibility shims                                  */
/*                                                                    */
/* The AOT emitter still generates these calls (unchanged in M2), so  */
/* they keep their exact signatures and drive the global text-driver  */
/* instance through the interface above.                              */
/* ------------------------------------------------------------------ */

static void shim_push(MisaWidget w) {
    if (!g_ui) return;
    if (g_ui->ssp >= g_ui->scap) {
        int32_t ncap = g_ui->scap ? g_ui->scap * 2 : 32;
        MisaWidget *ns = (MisaWidget *)realloc(g_ui->stack,
                                              sizeof(MisaWidget) * (size_t)ncap);
        if (!ns) misa_platform_fatal("E300", "out of memory");
        g_ui->stack = ns;
        g_ui->scap = ncap;
    }
    g_ui->stack[g_ui->ssp++] = w;
}

void misa_screen_begin(const char *name) {
    if (!g_ui) g_ui = misa_ui_create();
    MisaWidget root = misa_ui_begin_screen(g_ui, name);
    g_ui->ssp = 0;
    shim_push(root);
}

void misa_screen_comp_begin(const char *name, int has_args) {
    MisaWidget parent = g_ui->stack[g_ui->ssp - 1];
    MisaWidget w = misa_ui_begin_comp(g_ui, parent, name);
    /* Preserve the v0.3 incremental bytes: "(" was printed here when
     * the emitter declared args (see misa_ui_text_hint_args). */
    misa_ui_text_hint_args(g_ui, w, has_args);
    shim_push(w);
}

void misa_screen_comp_arg(const char *name, MVal *v, int is_first) {
    (void)is_first;
    misa_ui_set_arg(g_ui, g_ui->stack[g_ui->ssp - 1], name, v);
}

void misa_screen_comp_arg_handler(const char *name, int is_first,
                                  int handler_id) {
    (void)is_first;
    misa_ui_set_handler(g_ui, g_ui->stack[g_ui->ssp - 1], name,
                        handler_id);
}

/* Phase 4 (M4): bind the current component's id: slot. */
void misa_screen_comp_bind_id(int32_t slot) {
    if (!g_ui || g_ui->ssp <= 0) return;
    misa_ui_bind_id(g_ui, g_ui->stack[g_ui->ssp - 1], slot);
}

/* End of the head line; children follow, then comp_end.
 * Parens are closed lazily by the driver, so this is a no-op. */
void misa_screen_comp_head_end(int has_args) {
    (void)has_args;
}

void misa_screen_comp_end(void) {
    MisaWidget w = g_ui->stack[--g_ui->ssp];
    misa_ui_end_comp(g_ui, w);
}

void misa_screen_end(void) {
    misa_ui_show_screen(g_ui, g_ui->stack[0]);
    g_ui->ssp = 0;
}
