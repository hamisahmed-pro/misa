/* Misa AOT runtime — DOM UI driver (Phase 4, M3).
 *
 * Implements rt/misa_ui.h on the web: the screen call stream builds a
 * DOM tree (via rt/dom_bridge.js, linked with --js-library) inside
 * #misa-root. misa_ui_show_screen() swaps the new tree in.
 *
 * Widget handles are small ints; the JS side owns the elements. The 8
 * misa_screen_* functions are shims over a driver-global instance
 * (same shape as ui_text.c), so the emitter needs no per-driver
 * knowledge. Selected at link time by build.py's target table.
 */
#include "misa_ui.h"

#include "misa_platform.h"
#include "misa_rt.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* dom_bridge.js (linked via --js-library) */
extern int32_t misa_dom_create(const char *kind, int32_t parent);
extern void    misa_dom_set_text(int32_t dom, const char *text);
extern void    misa_dom_set_banner_style(int32_t dom, const char *style);
extern void    misa_dom_set_hint(int32_t dom, const char *hint);
extern void    misa_dom_set_image(int32_t dom, const char *path);
extern void    misa_dom_list_item(int32_t dom, const char *text);
extern void    misa_dom_set_handler(int32_t dom, const char *event,
                                    int32_t handler_id);
extern void    misa_dom_show(int32_t root);
extern char   *misa_dom_get_text(int32_t dom); /* malloc'd; free it */
extern void    misa_dom_free(char *p);
extern void    misa_dom_report_error(const char *text);

/* ------------------------------------------------------------------ */
/* driver state                                                       */
/* ------------------------------------------------------------------ */

typedef struct {
    int32_t dom;         /* dom_bridge.js element id, 0 = none */
    int32_t bound_slot;  /* id: registry slot, -1 = unbound */
    char    kind[24];    /* component kind ("TextInput", ...) */
} DomWidget;

struct MisaUI {
    DomWidget *widgets;  /* 1-based: widgets[id - 1] */
    int32_t    nw, wcap;
    /* shim call-stack (misa_screen_* layer) */
    MisaWidget *stack;
    int32_t     ssp, scap;
};

/* Process-global driver instance (this driver is selected at link
 * time; generated code reaches it via misa_ui_active()). */
static MisaUI *g_ui = NULL;

static void ui_grow_widgets(MisaUI *ui) {
    if (ui->nw < ui->wcap) return;
    int32_t ncap = ui->wcap ? ui->wcap * 2 : 16;
    DomWidget *nw = (DomWidget *)realloc(ui->widgets,
                                        sizeof(DomWidget) * (size_t)ncap);
    if (!nw) misa_platform_fatal("E300", "out of memory");
    ui->widgets = nw;
    ui->wcap = ncap;
}

static MisaWidget ui_new_widget(MisaUI *ui, const char *kind,
                                int32_t parent_dom) {
    ui_grow_widgets(ui);
    DomWidget *w = &ui->widgets[ui->nw++];
    w->dom = misa_dom_create(kind, parent_dom);
    w->bound_slot = -1;
    strncpy(w->kind, kind, sizeof w->kind - 1);
    w->kind[sizeof w->kind - 1] = '\0';
    return ui->nw; /* 1-based id */
}

static DomWidget *ui_widget(MisaUI *ui, MisaWidget w) {
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

MisaWidget misa_ui_begin_screen(MisaUI *ui, const char *name) {
    (void)name;
    ui->nw = 0; /* fresh widget namespace per screen */
    return ui_new_widget(ui, "__screen__", 0);
}

MisaWidget misa_ui_begin_comp(MisaUI *ui, MisaWidget parent,
                              const char *kind) {
    DomWidget *pw = ui_widget(ui, parent);
    int32_t pdom = pw ? pw->dom : 0;
    return ui_new_widget(ui, kind, pdom);
}

void misa_ui_set_arg(MisaUI *ui, MisaWidget w,
                     const char *arg, MVal *v) {
    DomWidget *dw = ui_widget(ui, w);
    if (!dw || !dw->dom) return;
    if (strcmp(arg, "items") == 0 && misa_tag(v) == M_TAG_LIST) {
        /* List(items:): one <li> per item, rendered with the
         * existing misa_render (no new formatting rules). */
        MVal *it = misa_iter_new(v, 0, 0);
        if (misa_pending()) return; /* cannot happen: tag checked */
        for (;;) {
            MVal *x = misa_iter_next(it);
            if (!x) break;
            MVal *rt = misa_render(x);
            misa_dom_list_item(dw->dom, misa_text_z(rt));
        }
        return;
    }
    if (strcmp(arg, "photo") == 0) {
        if (misa_media_kind(v) >= 0)
            misa_dom_set_image(dw->dom, misa_media_path(v));
        return;
    }
    MVal *t = misa_render(v);
    const char *z = misa_text_z(t);
    if (strcmp(arg, "text") == 0 || strcmp(arg, "label") == 0) {
        misa_dom_set_text(dw->dom, z);
    } else if (strcmp(arg, "hint") == 0) {
        misa_dom_set_hint(dw->dom, z);
    } else if (strcmp(arg, "style") == 0) {
        misa_dom_set_banner_style(dw->dom, z);
    }
    /* unknown args are ignored (checker constrains the set) */
}

void misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                         const char *event, int32_t handler_id) {
    DomWidget *dw = ui_widget(ui, w);
    if (!dw || !dw->dom) return;
    misa_dom_set_handler(dw->dom, event, handler_id);
}

void misa_ui_end_comp(MisaUI *ui, MisaWidget w) {
    (void)ui; (void)w;
    /* the tree is assembled in the DOM as it is built; nothing to do */
}

void misa_ui_bind_id(MisaUI *ui, MisaWidget w, int32_t slot) {
    DomWidget *dw = ui_widget(ui, w);
    if (dw) dw->bound_slot = slot;
}

void misa_ui_show_screen(MisaUI *ui, MisaWidget root) {
    DomWidget *rw = ui_widget(ui, root);
    misa_dom_show(rw ? rw->dom : 0);
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

MVal *misa_ui_get_text(MisaUI *ui, MisaWidget w) {
    DomWidget *dw = ui_widget(ui, w);
    if (!dw || !dw->dom || strcmp(dw->kind, "TextInput") != 0) {
        misa_raise("E201", "cannot read '.text': no TextInput is "
                   "bound to this id on the current screen", 0, 0);
        return NULL;
    }
    char *z = misa_dom_get_text(dw->dom);
    MVal *t = misa_text_cstr(z ? z : "");
    if (z) misa_dom_free(z);
    return t;
}

/* Phase 4 (M4): dispatch a compiled event handler through the
 * generated misa_handler_table[]. Returns 0 when the handler ran
 * clean, 1 when it raised (reported via misa_ui_report_error — the
 * page is never killed), -1 for an unknown handler id. */
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

/* CameraButton helper, called from dom_bridge.js after staging the
 * chosen file into the Emscripten FS: builds the Photo handle the
 * onCapture handler receives. Exported to JS (see build.py). */
MVal *misa_dom_photo(const char *vpath) {
    return misa_media(0 /* Photo */, vpath ? vpath : "");
}

/* Handler-boundary errors: a dismissible banner + console, never
 * killing the page. */
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
    snprintf(buf, sizeof buf, "error %s: %s", code, msg);
    misa_dom_report_error(buf);
}

/* ------------------------------------------------------------------ */
/* misa_screen_* compatibility shims (same shape as ui_text.c)        */
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
    (void)has_args;
    MisaWidget parent = g_ui->stack[g_ui->ssp - 1];
    MisaWidget w = misa_ui_begin_comp(g_ui, parent, name);
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

void misa_screen_comp_bind_id(int32_t slot) {
    if (!g_ui || g_ui->ssp <= 0) return;
    misa_ui_bind_id(g_ui, g_ui->stack[g_ui->ssp - 1], slot);
}

/* End of the head line; children follow, then comp_end. */
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
