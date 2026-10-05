/* Misa AOT runtime — JNI entry for the Android target (Phase 4, M6).
 *
 * Doc §1.1 B6: JNI_OnLoad + native methods. The emitter selects the
 * entry per target (build.py TARGETS: android rows use "misa_ui_boot",
 * the same entry shape as the web target); the native boot method
 * below invokes the emitted misa_ui_boot(), which runs misa_rt_init()
 * + misa_main() with the Android Views driver active.
 *
 * All native methods are called on the "Misa" HandlerThread owned by
 * MisaActivity (see jni_android.h for the threading model), so they
 * receive a valid JNIEnv directly.
 */
#include "jni_android.h"

#include "misa_rt.h"   /* misa_media */
#include "misa_ui.h"   /* misa_dispatch_handler (driver-owned) */

#include <stdlib.h>
#include <string.h>

/* The program entry the AOT emitter generates (entry="misa_ui_boot"
 * in build.py's android target rows): misa_rt_init() + misa_main(),
 * uncaught errors routed to the driver. Defined in prog.o. */
extern void misa_ui_boot(void);

static JavaVM *g_vm = NULL;
static jclass  g_widgets_class = NULL; /* global ref */
static jobject g_widgets = NULL;       /* global ref */

static jmethodID g_mid_create;
static jmethodID g_mid_set_text;
static jmethodID g_mid_set_hint;
static jmethodID g_mid_set_banner_style;
static jmethodID g_mid_set_image;
static jmethodID g_mid_list_item;
static jmethodID g_mid_set_handler;
static jmethodID g_mid_show;
static jmethodID g_mid_get_text;
static jmethodID g_mid_report_error;
static jmethodID g_mid_report_fatal;

/* P4: app files dir, supplied by MisaActivity at boot. */
static char g_files_dir[4096] = "";

/* P3: app-supplied config table (fixed, small; keys from the app,
 * never baked into the binary). */
#define MISA_CFG_MAX 32
static struct {
    char key[128];
    char val[4096];
} g_cfg[MISA_CFG_MAX];
static int g_ncfg = 0;

/* ------------------------------------------------------------------ */
/* accessors (jni_android.h)                                          */
/* ------------------------------------------------------------------ */

JavaVM *misa_android_vm(void) { return g_vm; }

JNIEnv *misa_android_jnienv(void) {
    JNIEnv *env = NULL;
    if (!g_vm) return NULL;
    if ((*g_vm)->GetEnv(g_vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK) {
        if ((*g_vm)->AttachCurrentThread(g_vm, &env, NULL) != JNI_OK)
            return NULL;
    }
    return env;
}

jobject misa_android_widgets(void) { return g_widgets; }

jmethodID misa_mid_create(void)         { return g_mid_create; }
jmethodID misa_mid_set_text(void)       { return g_mid_set_text; }
jmethodID misa_mid_set_hint(void)       { return g_mid_set_hint; }
jmethodID misa_mid_set_banner_style(void) { return g_mid_set_banner_style; }
jmethodID misa_mid_set_image(void)      { return g_mid_set_image; }
jmethodID misa_mid_list_item(void)      { return g_mid_list_item; }
jmethodID misa_mid_set_handler(void)    { return g_mid_set_handler; }
jmethodID misa_mid_show(void)           { return g_mid_show; }
jmethodID misa_mid_get_text(void)       { return g_mid_get_text; }
jmethodID misa_mid_report_error(void)   { return g_mid_report_error; }
jmethodID misa_mid_report_fatal(void)   { return g_mid_report_fatal; }

void misa_android_fatal_ui(const char *text) {
    JNIEnv *env = misa_android_jnienv();
    if (!env || !g_widgets || !g_mid_report_fatal) return;
    jstring js = (*env)->NewStringUTF(env, text ? text : "fatal");
    if (!js) return;
    (*env)->CallVoidMethod(env, g_widgets, g_mid_report_fatal, js);
    (*env)->DeleteLocalRef(env, js);
    /* Java-side: toast + activity.finish() on the UI thread. */
}

const char *misa_android_config_get(const char *name) {
    if (!name) return NULL;
    for (int i = 0; i < g_ncfg; i++) {
        if (strcmp(g_cfg[i].key, name) == 0)
            return g_cfg[i].val;
    }
    return NULL;
}

void misa_android_config_set(const char *name, const char *value) {
    if (!name || !value) return;
    for (int i = 0; i < g_ncfg; i++) {
        if (strcmp(g_cfg[i].key, name) == 0) {
            strncpy(g_cfg[i].val, value, sizeof g_cfg[i].val - 1);
            g_cfg[i].val[sizeof g_cfg[i].val - 1] = '\0';
            return;
        }
    }
    if (g_ncfg >= MISA_CFG_MAX) return; /* table full: drop, never grow */
    strncpy(g_cfg[g_ncfg].key, name, sizeof g_cfg[g_ncfg].key - 1);
    g_cfg[g_ncfg].key[sizeof g_cfg[g_ncfg].key - 1] = '\0';
    strncpy(g_cfg[g_ncfg].val, value, sizeof g_cfg[g_ncfg].val - 1);
    g_cfg[g_ncfg].val[sizeof g_cfg[g_ncfg].val - 1] = '\0';
    g_ncfg++;
}

const char *misa_android_files_dir(void) { return g_files_dir; }

/* ------------------------------------------------------------------ */
/* JNI_OnLoad                                                         */
/* ------------------------------------------------------------------ */

jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    g_vm = vm;
    JNIEnv *env = misa_android_jnienv();
    if (!env) return JNI_ERR;
    jclass local = (*env)->FindClass(env, "com/misa/app/MisaWidgets");
    if (!local) return JNI_ERR;
    g_widgets_class = (*env)->NewGlobalRef(env, local);
    (*env)->DeleteLocalRef(env, local);
    if (!g_widgets_class) return JNI_ERR;

#define MID(var, name, sig)                                             \
    do {                                                                \
        g_##var = (*env)->GetMethodID(env, g_widgets_class, name, sig); \
        if (!g_##var) return JNI_ERR;                                   \
    } while (0)

    MID(mid_create, "create", "(Ljava/lang/String;I)I");
    MID(mid_set_text, "setText", "(ILjava/lang/String;)V");
    MID(mid_set_hint, "setHint", "(ILjava/lang/String;)V");
    MID(mid_set_banner_style, "setBannerStyle", "(ILjava/lang/String;)V");
    MID(mid_set_image, "setImage", "(ILjava/lang/String;)V");
    MID(mid_list_item, "listItem", "(ILjava/lang/String;)V");
    MID(mid_set_handler, "setHandler", "(ILjava/lang/String;I)V");
    MID(mid_show, "show", "(I)V");
    MID(mid_get_text, "getText", "(I)Ljava/lang/String;");
    MID(mid_report_error, "reportError", "(Ljava/lang/String;)V");
    MID(mid_report_fatal, "reportFatal", "(Ljava/lang/String;)V");
#undef MID
    return JNI_VERSION_1_6;
}

/* ------------------------------------------------------------------ */
/* native methods: com.misa.app.MisaNative                            */
/* ------------------------------------------------------------------ */

static void set_files_dir(JNIEnv *env, jstring jdir) {
    g_files_dir[0] = '\0';
    if (!jdir) return;
    const char *c = (*env)->GetStringUTFChars(env, jdir, NULL);
    if (c) {
        strncpy(g_files_dir, c, sizeof g_files_dir - 1);
        g_files_dir[sizeof g_files_dir - 1] = '\0';
        (*env)->ReleaseStringUTFChars(env, jdir, c);
    }
}

/* Boot: widgets + files dir are supplied by the Activity (which posts
 * this to the Misa HandlerThread). Invokes the emitted misa_ui_boot()
 * — misa_rt_init() + misa_main() with the Android driver active. */
void Java_com_misa_app_MisaNative_nativeBoot(JNIEnv *env, jclass clazz,
                                             jobject widgets,
                                             jstring filesDir) {
    (void)clazz;
    if (widgets) {
        if (g_widgets) (*env)->DeleteGlobalRef(env, g_widgets);
        g_widgets = (*env)->NewGlobalRef(env, widgets);
    }
    set_files_dir(env, filesDir);
    misa_ui_boot();
}

/* UI-event dispatch (posted to the Misa thread by MisaWidgets). */
void Java_com_misa_app_MisaNative_nativeDispatch(JNIEnv *env, jclass clazz,
                                                 jint handlerId) {
    (void)env; (void)clazz;
    misa_dispatch_handler(handlerId, NULL);
}

/* CameraButton result (posted to the Misa thread by MisaActivity):
 * builds the Photo handle the onCapture handler receives. The uri is
 * a content:// URI string; the Java side of setImage/getImage loads
 * it through the ContentResolver. */
void Java_com_misa_app_MisaNative_nativePhotoResult(JNIEnv *env, jclass clazz,
                                                    jstring uri,
                                                    jint handlerId) {
    (void)clazz;
    const char *u = uri ? (*env)->GetStringUTFChars(env, uri, NULL) : NULL;
    MVal *photo = misa_media(0 /* Photo */, u ? u : "");
    if (u) (*env)->ReleaseStringUTFChars(env, uri, u);
    misa_dispatch_handler(handlerId, photo);
}

/* P3: app-supplied config (keys from the app, never baked in). */
void Java_com_misa_app_MisaNative_nativeSetConfig(JNIEnv *env, jclass clazz,
                                                  jstring key,
                                                  jstring value) {
    (void)clazz;
    if (!key || !value) return;
    const char *k = (*env)->GetStringUTFChars(env, key, NULL);
    const char *v = (*env)->GetStringUTFChars(env, value, NULL);
    if (k && v) misa_android_config_set(k, v);
    if (k) (*env)->ReleaseStringUTFChars(env, key, k);
    if (v) (*env)->ReleaseStringUTFChars(env, value, v);
}
