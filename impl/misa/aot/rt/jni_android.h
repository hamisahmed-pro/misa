/* Misa AOT runtime — Android JNI plumbing (Phase 4, M6).
 *
 * Internal header shared by jni_android.c (which owns the state),
 * platform_android.c (P1-P4 + HTTP transport) and ui_android.c (the
 * Views driver). Everything runs on the "Misa" HandlerThread owned by
 * MisaActivity; the thread is a Java thread, so it is already attached
 * to the VM when the native methods run.
 *
 * Threading model (documented once, here):
 *   - MisaActivity owns a HandlerThread("Misa"). EVERY native entry
 *     (boot, UI-event dispatch, camera result) is posted to it.
 *   - View operations from the Misa thread marshal to the UI thread
 *     inside MisaWidgets (runOnUiThread + latch when a result is
 *     needed; direct call when already on the UI thread).
 *   - Network (HttpURLConnection) therefore always runs on the Misa
 *     thread — never the UI thread (no NetworkOnMainThreadException)
 *     — and handlers never run on the UI thread (no ANR).
 *
 * Not for external use (no misa_ prefix on most of it, deliberately).
 */
#ifndef MISA_JNI_ANDROID_H
#define MISA_JNI_ANDROID_H

#include <jni.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The VM, cached by JNI_OnLoad. */
JavaVM *misa_android_vm(void);

/* JNIEnv for the calling thread; attaches if needed (returns NULL
 * only when the attach fails). */
JNIEnv *misa_android_jnienv(void);

/* Global ref to the MisaWidgets instance (the Java side of the UI
 * driver), set by nativeBoot before misa_ui_boot() runs. */
jobject misa_android_widgets(void);

/* Cached MisaWidgets method ids (valid after JNI_OnLoad): */
jmethodID misa_mid_create(void);        /* (String, int) -> int view id */
jmethodID misa_mid_set_text(void);      /* (int, String) -> void */
jmethodID misa_mid_set_hint(void);      /* (int, String) -> void */
jmethodID misa_mid_set_banner_style(void); /* (int, String) -> void */
jmethodID misa_mid_set_image(void);     /* (int, String) -> void */
jmethodID misa_mid_list_item(void);     /* (int, String) -> void */
jmethodID misa_mid_set_handler(void);   /* (int, String, int) -> void */
jmethodID misa_mid_show(void);          /* (int) -> void */
jmethodID misa_mid_get_text(void);      /* (int) -> String */
jmethodID misa_mid_report_error(void);  /* (String) -> void */
jmethodID misa_mid_report_fatal(void);  /* (String) -> void */

/* P2 support: toast + activity.finish(), marshalled to the UI thread
 * by the Java side. Never returns to the caller in spirit — the
 * caller (misa_platform_fatal) parks the Misa thread afterwards. */
void misa_android_fatal_ui(const char *text);

/* P3: app-supplied config table (set from the Activity via
 * nativeSetConfig before boot; keys are never baked into the
 * binary). Returns NULL when unset; the pointer has static lifetime. */
const char *misa_android_config_get(const char *name);
void        misa_android_config_set(const char *name, const char *value);

/* P4: the app's files dir (set from the Activity via nativeBoot).
 * Static lifetime; "" when unset (P4 then fails closed). */
const char *misa_android_files_dir(void);

#ifdef __cplusplus
}
#endif
#endif /* MISA_JNI_ANDROID_H */
