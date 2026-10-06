package com.misa.app;

import android.content.Context;

/**
 * TFLite stub (W-3): compiled in place of the real MisaTflite when the
 * program declares no .tflite model, so non-ML APKs don't carry the
 * TensorFlow Lite runtime (~1MB+ of classes + native .so).
 *
 * Same public API as android/java/com/misa/app/MisaTflite.java;
 * MisaActivity calls init() unconditionally, which is a no-op here.
 * infer() can only be reached via JNI when native code handles a
 * .tflite model call — impossible without a declared model — so it
 * fails with a clean E502 rather than a linkage error.
 */
public final class MisaTflite {
    private MisaTflite() {}

    /** Called once from MisaActivity.onCreate; no-op without TFLite. */
    public static void init(Context ctx) {}

    public static String infer(String assetName, String inputJson,
                               String namesJson) {
        throw new RuntimeException(
                "E502: no .tflite model is bundled with this app");
    }
}
