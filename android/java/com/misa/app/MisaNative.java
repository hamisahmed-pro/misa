package com.misa.app;

/** JNI bridge to libmisa.so (the AOT-compiled Misa program + runtime).
 *
 * All native methods run on the "Misa" HandlerThread owned by
 * MisaActivity — never on the UI thread (see rt/jni_android.h for the
 * threading model).
 */
public final class MisaNative {
    static {
        System.loadLibrary("misa");
    }

    private MisaNative() {}

    /** Boot the Misa program: misa_rt_init() + misa_main() with the
     * Android Views driver active. widgets = the MisaWidgets instance
     * owning the Views; filesDir = the app's private files dir (P4). */
    public static native void nativeBoot(MisaWidgets widgets,
                                         String filesDir);

    /** Dispatch a compiled event handler (button tap). */
    public static native void nativeDispatch(int handlerId);

    /** CameraButton result: the captured photo's content:// URI and
     * the onCapture handler id. */
    public static native void nativePhotoResult(String uri,
                                                int handlerId);

    /** P3: app-supplied config (provider keys etc.). Keys come from
     * the app — intent extras / SharedPreferences — and are NEVER
     * baked into the native library. */
    public static native void nativeSetConfig(String key, String value);
}
