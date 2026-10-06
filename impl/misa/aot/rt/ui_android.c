/* Misa AOT runtime — Android Views UI driver (Phase 4, M6).
 *
 * Implements rt/misa_ui.h on Android. Every operation forwards through
 * JNI to the Java-side com.misa.app.MisaWidgets (owned by the
 * MisaActivity), which owns the real Views. Widget handles ARE the
 * Java view ids (1-based ints) — the C side keeps only kind strings
 * for readback checks and the id: binding registry.
 *
 * Component mapping (doc §4.4):
 *   Column       -> LinearLayout (vertical)
 *   Row          -> LinearLayout (horizontal)
 *   Spacer       -> View, layout_weight=1
 *   Text         -> TextView
 *   Heading      -> TextView (larger, bold)
 *   Banner       -> TextView tinted by style (info/warning/error)
 *   Button       -> Button (+ OnClickListener -> misa_dispatch_handler)
 *   TextInput    -> EditText (+ get_text readback)
 *   CameraButton -> Button + ACTION_IMAGE_CAPTURE intent; the captured
 *                   photo's content:// URI becomes the Photo handle for
 *                   the onCapture handler
 *   Image        -> ImageView
 *   List         -> LinearLayout (vertical) of TextViews, one per item
 *                   (M6 slice: RecyclerView needs the androidx aar; the
 *                   single-text-row shape is identical, recycling is a
 *                   later optimization — see docs/android.md)
 *
 * An Activity hosts one MisaUI. All driver calls run on the Misa
 * thread (see jni_android.h); MisaWidgets marshals view work to the
 * UI thread itself.
 */
#include "misa_ui.h"

#include "jni_android.h"
#include "misa_platform.h"
#include "misa_rt.h"

#include <jni.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* driver state                                                       */
/* ------------------------------------------------------------------ */

typedef struct {
    int32_t view;        /* Java view id from MisaWidgets.create() */
    char    kind[32];    /* component kind ("TextInput", ...) */
    int32_t bound_slot;  /* id: registry slot, -1 = unbound */
} AndroidWidget;

struct MisaUI {
    AndroidWidget *widgets;  /* 1-based: widgets[id - 1] */
    int32_t        nw, wcap;
    /* shim call-stack (misa_screen_* layer) */
    MisaWidget    *stack;
    int32_t        ssp, scap;
};

/* Process-global driver instance (selected at link time; generated
 * code reaches it via misa_ui_active()). */
static MisaUI *g_ui = NULL;

/* Dispatches arrive on the Misa thread by design, but a mutex keeps
 * the contract explicit if a platform ever posts one elsewhere. */
static pthread_mutex_t g_dispatch_mu = PTHREAD_MUTEX_INITIALIZER;

static void ui_grow_widgets(MisaUI *ui) {
    if (ui->nw < ui->wcap) return;
    int32_t ncap = ui->wcap ? ui->wcap * 2 : 16;
    AndroidWidget *nw = (AndroidWidget *)realloc(
        ui->widgets, sizeof(AndroidWidget) * (size_t)ncap);
    if (!nw) misa_platform_fatal("E300", "out of memory");
    ui->widgets = nw;
    ui->wcap = ncap;
}

/* Call a (int, String) -> void widget method. */
static void jcall_is(JNIEnv *env, jobject w, jmethodID mid,
                     int32_t id, const char *text) {
    jstring js = (*env)->NewStringUTF(env, text ? text : "");
    if (!js) return;
    (*env)->CallVoidMethod(env, w, mid, (jint)id, js);
    (*env)->DeleteLocalRef(env, js);
    if ((*env)->ExceptionOccurred(env)) (*env)->ExceptionClear(env);
}

/* Call a (String) -> void widget method. */
static void jcall_s(JNIEnv *env, jobject w, jmethodID mid,
                    const char *text) {
    jstring js = (*env)->NewStringUTF(env, text ? text : "");
    if (!js) return;
    (*env)->CallVoidMethod(env, w, mid, js);
    (*env)->DeleteLocalRef(env, js);
    if ((*env)->ExceptionOccurred(env)) (*env)->ExceptionClear(env);
}

static int32_t ui_new_widget(MisaUI *ui, const char *kind,
                             int32_t parent) {
    ui_grow_widgets(ui);
    int32_t id = 0;
    JNIEnv *env = misa_android_jnienv();
    jobject w = misa_android_widgets();
    if (env && w) {
        jstring jk = (*env)->NewStringUTF(env, kind);
        if (jk) {
            id = (*env)->CallIntMethod(env, w, misa_mid_create(),
                                       jk, (jint)parent);
            (*env)->DeleteLocalRef(env, jk);
            if ((*env)->ExceptionOccurred(env)) {
                (*env)->ExceptionClear(env);
                id = 0;
            }
        }
    }
    AndroidWidget *aw = &ui->widgets[ui->nw++];
    aw->view = id; /* Java view id; 0 = Java-side creation failed */
    strncpy(aw->kind, kind, sizeof aw->kind - 1);
    aw->kind[sizeof aw->kind - 1] = '\0';
    aw->bound_slot = -1;
    return ui->nw; /* 1-based C handle; aw->view is the Java id */
}

static AndroidWidget *ui_widget(MisaUI *ui, MisaWidget w) {
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
    AndroidWidget *pw = ui_widget(ui, parent);
    int32_t pview = pw ? pw->view : 0;
    return ui_new_widget(ui, kind, pview);
}

void misa_ui_set_arg(MisaUI *ui, MisaWidget w,
                     const char *arg, MVal *v) {
    AndroidWidget *aw = ui_widget(ui, w);
    if (!aw || aw->view <= 0) return;
    JNIEnv *env = misa_android_jnienv();
    jobject jw = misa_android_widgets();
    if (!env || !jw) return;
    if (strcmp(arg, "items") == 0 && misa_tag(v) == M_TAG_LIST) {
        /* List(items:): one TextView row per item, rendered with the
         * existing misa_render (no new formatting rules). */
        MVal *it = misa_iter_new(v, 0, 0);
        if (misa_pending()) return; /* cannot happen: tag checked */
        for (;;) {
            MVal *x = misa_iter_next(it);
            if (!x) break;
            MVal *rt = misa_render(x);
            jcall_is(env, jw, misa_mid_list_item(), aw->view,
                     misa_text_z(rt));
        }
        return;
    }
    if (strcmp(arg, "photo") == 0) {
        if (misa_media_kind(v) >= 0)
            jcall_is(env, jw, misa_mid_set_image(), aw->view,
                     misa_media_path(v));
        return;
    }
    MVal *t = misa_render(v);
    const char *z = misa_text_z(t);
    if (strcmp(arg, "text") == 0 || strcmp(arg, "label") == 0) {
        jcall_is(env, jw, misa_mid_set_text(), aw->view, z);
    } else if (strcmp(arg, "hint") == 0) {
        jcall_is(env, jw, misa_mid_set_hint(), aw->view, z);
    } else if (strcmp(arg, "style") == 0) {
        jcall_is(env, jw, misa_mid_set_banner_style(), aw->view, z);
    }
    /* unknown args are ignored (checker constrains the set) */
}

void misa_ui_set_handler(MisaUI *ui, MisaWidget w,
                         const char *event, int32_t handler_id) {
    AndroidWidget *aw = ui_widget(ui, w);
    if (!aw || aw->view <= 0) return;
    JNIEnv *env = misa_android_jnienv();
    jobject jw = misa_android_widgets();
    if (!env || !jw) return;
    jstring je = (*env)->NewStringUTF(env, event);
    if (!je) return;
    (*env)->CallVoidMethod(env, jw, misa_mid_set_handler(),
                           (jint)aw->view, je, (jint)handler_id);
    (*env)->DeleteLocalRef(env, je);
    if ((*env)->ExceptionOccurred(env)) (*env)->ExceptionClear(env);
}

void misa_ui_end_comp(MisaUI *ui, MisaWidget w) {
    (void)ui; (void)w;
    /* the tree is assembled on the Java side as it is built */
}

void misa_ui_bind_id(MisaUI *ui, MisaWidget w, int32_t slot) {
    AndroidWidget *aw = ui_widget(ui, w);
    if (aw) aw->bound_slot = slot;
}

void misa_ui_show_screen(MisaUI *ui, MisaWidget root) {
    AndroidWidget *aw = ui_widget(ui, root);
    if (!aw || aw->view <= 0) return;
    JNIEnv *env = misa_android_jnienv();
    jobject jw = misa_android_widgets();
    if (!env || !jw) return;
    (*env)->CallVoidMethod(env, jw, misa_mid_show(), (jint)aw->view);
    if ((*env)->ExceptionOccurred(env)) (*env)->ExceptionClear(env);
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
    AndroidWidget *aw = ui_widget(ui, w);
    if (!aw || strcmp(aw->kind, "TextInput") != 0) {
        misa_raise("E201", "cannot read '.text': no TextInput is "
                   "bound to this id on the current screen", 0, 0);
        return NULL;
    }
    JNIEnv *env = misa_android_jnienv();
    jobject jw = misa_android_widgets();
    MVal *t = misa_text_cstr("");
    if (env && jw) {
        jstring js = (jstring)(*env)->CallObjectMethod(
            env, jw, misa_mid_get_text(), (jint)aw->view);
        if (!(*env)->ExceptionOccurred(env) && js) {
            const char *c = (*env)->GetStringUTFChars(env, js, NULL);
            if (c) {
                t = misa_text_cstr(c);
                (*env)->ReleaseStringUTFChars(env, js, c);
            }
            (*env)->DeleteLocalRef(env, js);
        } else {
            (*env)->ExceptionClear(env);
        }
    }
    return t;
}

/* Phase 4 (M4): dispatch a compiled event handler through the
 * generated misa_handler_table[]. Returns 0 when the handler ran
 * clean, 1 when it raised (reported via misa_ui_report_error — the
 * app is never killed), -1 for an unknown handler id. */
int32_t misa_dispatch_handler(int32_t handler_id, MVal *event) {
    int32_t rc;
    pthread_mutex_lock(&g_dispatch_mu);
    if (handler_id < 0 || handler_id >= misa_num_handlers) {
        rc = -1;
    } else {
        MVal *r = misa_handler_table[handler_id](event);
        (void)r; /* handler return values are ignored */
        if (misa_pending()) {
            MVal *err = misa_take_pending();
            misa_ui_report_error(misa_ui_active(), err);
            rc = 1;
        } else {
            rc = 0;
        }
        /* Screen state: re-render if a state cell was mutated. */
        misa_screen_after_handler();
    }
    pthread_mutex_unlock(&g_dispatch_mu);
    return rc;
}

/* Handler-boundary errors: toast + logcat, never killing the app. */
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
    JNIEnv *env = misa_android_jnienv();
    jobject jw = misa_android_widgets();
    if (env && jw)
        jcall_s(env, jw, misa_mid_report_error(), buf);
}

/* ------------------------------------------------------------------ */
/* misa_screen_* compatibility shims (same shape as ui_dom.c)        */
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
