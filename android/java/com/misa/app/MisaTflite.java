package com.misa.app;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

import org.json.JSONArray;
import org.tensorflow.lite.DataType;
import org.tensorflow.lite.Interpreter;
import org.tensorflow.lite.InterpreterApi;
import org.tensorflow.lite.Tensor;
import org.tensorflow.lite.nnapi.NnApiDelegate;

/**
 * On-device TFLite inference (Phase 5, M4).
 *
 * Called from native code (platform_android.c, misa_tflite_infer) via JNI:
 *   MisaTflite.infer(assetName, inputJson, namesJson) -> outputJson
 *
 * - assetName: model file name inside the APK assets (e.g.
 *   "tiny_classifier.tflite"); staged into the app files dir on first use.
 * - inputJson: JSON array with one entry per model input, in declaration
 *   order (numbers, arrays of numbers, or strings — the v1 type mapping:
 *   Float / [Float] / Text).
 * - namesJson: JSON array of the declared Misa param names, for E501
 *   messages.
 * - returns: JSON of the FIRST model output — a number, a flat array of
 *   numbers, or a string (mirrors the interpreter's _from_output).
 *
 * The NNAPI delegate is used when the device can actually run the model
 * on it (verified by a zero-input probe at load); otherwise the
 * interpreter falls back to CPU. Any failure throws RuntimeException;
 * model-compat failures carry an "E501: ..."/"E502: ..." prefix that the
 * native side turns into the matching E-code, everything else becomes
 * E400.
 */
public final class MisaTflite {
    private static final String TAG = "MISA";

    private MisaTflite() {}

    private static Context appContext;
    private static final Map<String, Interpreter> CACHE = new HashMap<>();

    /** Called once from MisaActivity.onCreate, before nativeBoot. */
    public static void init(Context ctx) {
        appContext = ctx.getApplicationContext();
    }

    public static String infer(String assetName, String inputJson,
                               String namesJson) {
        try {
            return inferInner(assetName, inputJson, namesJson);
        } catch (RuntimeException e) {
            throw e; // E50x prefixes pass through untouched
        } catch (Exception e) {
            throw new RuntimeException("TFLite inference failed: "
                    + e.getMessage(), e);
        }
    }

    private static String inferInner(String assetName, String inputJson,
                                     String namesJson) throws Exception {
        Interpreter itp = interpreterFor(assetName);
        JSONArray inputs = new JSONArray(inputJson);
        JSONArray names = new JSONArray(namesJson);
        int nIn = itp.getInputTensorCount();
        if (inputs.length() != nIn) {
            throw new RuntimeException("E502: model takes " + nIn
                    + " input(s), called with " + inputs.length()
                    + " arg(s)");
        }
        Object[] tfInputs = new Object[nIn];
        for (int i = 0; i < nIn; i++) {
            String pname = names.length() > i ? names.getString(i)
                    : ("input" + i);
            tfInputs[i] = toTensorInput(itp.getInputTensor(i),
                    inputs.get(i), pname);
        }
        int nOut = itp.getOutputTensorCount();
        if (nOut < 1) {
            throw new RuntimeException("E502: model has no outputs");
        }
        Tensor outT = itp.getOutputTensor(0);
        Object outBuf = emptyOutput(outT);
        Map<Integer, Object> outputs = new HashMap<>();
        outputs.put(0, outBuf);
        itp.runForMultipleInputsOutputs(tfInputs, outputs);
        return outputJson(outT, outBuf);
    }

    // ------------------------------------------------------------------
    // model loading + NNAPI
    // ------------------------------------------------------------------

    private static Interpreter interpreterFor(String assetName)
            throws Exception {
        synchronized (CACHE) {
            Interpreter itp = CACHE.get(assetName);
            if (itp != null) return itp;
            if (appContext == null) {
                throw new RuntimeException(
                        "TFLite: MisaTflite.init was not called");
            }
            File dir = new File(appContext.getFilesDir(), "misa_models");
            if (!dir.isDirectory() && !dir.mkdirs()) {
                throw new RuntimeException(
                        "TFLite: cannot create model dir");
            }
            File f = new File(dir, new File(assetName).getName());
            if (!f.isFile()) {
                copyAsset(assetName, f);
            }
            itp = createWithFallback(f, assetName);
            CACHE.put(assetName, itp);
            return itp;
        }
    }

    private static void copyAsset(String assetName, File dst)
            throws Exception {
        try (InputStream in = appContext.getAssets().open(assetName);
             FileOutputStream out = new FileOutputStream(dst)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } catch (Exception e) {
            throw new RuntimeException("TFLite: cannot stage model asset '"
                    + assetName + "': " + e.getMessage(), e);
        }
    }

    /**
     * Try the NNAPI delegate first; verify it can actually run this
     * model with a zero-input probe; fall back to CPU otherwise.
     */
    private static Interpreter createWithFallback(File f, String assetName) {
        Throwable nnapiErr = null;
        try {
            Interpreter.Options opts = new Interpreter.Options();
            // Never reach for the Play Services TFLite client; the
            // bundled libtensorflowlite_jni.so is the runtime.
            opts.setRuntime(InterpreterApi.Options.TfLiteRuntime
                    .FROM_APPLICATION_ONLY);
            NnApiDelegate delegate = new NnApiDelegate();
            opts.addDelegate(delegate);
            Interpreter itp = new Interpreter(f, opts);
            try {
                probe(itp);
            } catch (Throwable t) {
                try { itp.close(); } catch (Throwable ignored) {}
                try { delegate.close(); } catch (Throwable ignored) {}
                throw t;
            }
            Log.i(TAG, "TFLite NNAPI delegate active for " + assetName);
            return itp;
        } catch (Throwable t) {
            nnapiErr = t;
        }
        Log.i(TAG, "TFLite CPU fallback for " + assetName
                + " (NNAPI: " + nnapiErr + ")");
        Interpreter.Options cpuOpts = new Interpreter.Options();
        cpuOpts.setRuntime(InterpreterApi.Options.TfLiteRuntime
                .FROM_APPLICATION_ONLY);
        return new Interpreter(f, cpuOpts);
    }

    /** Zero-input probe: proves the delegate can run this model. */
    private static void probe(Interpreter itp) throws Exception {
        int nIn = itp.getInputTensorCount();
        Object[] inputs = new Object[nIn];
        for (int i = 0; i < nIn; i++) {
            inputs[i] = zeroInput(itp.getInputTensor(i));
        }
        int nOut = itp.getOutputTensorCount();
        Map<Integer, Object> outputs = new HashMap<>();
        for (int i = 0; i < nOut; i++) {
            outputs.put(i, emptyOutput(itp.getOutputTensor(i)));
        }
        itp.runForMultipleInputsOutputs(inputs, outputs);
    }

    // ------------------------------------------------------------------
    // tensor marshalling (v1 type mapping)
    // ------------------------------------------------------------------

    private static int numElements(int[] shape) {
        int n = 1;
        for (int d : shape) n *= d;
        return n;
    }

    private static Object zeroInput(Tensor t) {
        if (t.dataType() == DataType.STRING) {
            int[] shape = t.shape();
            if (shape.length == 1) {
                return new byte[][]{new byte[0]};
            }
            return new byte[0];
        }
        int n = numElements(t.shape());
        ByteBuffer bb = ByteBuffer.allocateDirect(n * 4)
                .order(ByteOrder.nativeOrder());
        for (int i = 0; i < n; i++) bb.putFloat(0f);
        bb.rewind();
        return bb;
    }

    private static Object toTensorInput(Tensor t, Object jsonVal,
                                        String pname) {
        DataType dt = t.dataType();
        int[] shape = t.shape();
        int n = numElements(shape);
        if (dt == DataType.STRING) {
            if (!(jsonVal instanceof String)) {
                throw new RuntimeException("E502: param '" + pname
                        + "': Text value required");
            }
            byte[] bytes = ((String) jsonVal).getBytes(
                    StandardCharsets.UTF_8);
            // v1 string tensors are [1] (mirrors the ORT backend);
            // 0-d would be a bare byte[].
            if (shape.length == 0) {
                return bytes;
            }
            if (shape.length == 1 && n == 1) {
                return new byte[][]{bytes};
            }
            throw new RuntimeException("E502: param '" + pname
                    + "': model input has unexpected string shape "
                    + "(v1: [1])");
        }
        if (dt != DataType.FLOAT32) {
            throw new RuntimeException("E502: param '" + pname
                    + "': model input is not a float tensor "
                    + "(v1 supports float tensors only)");
        }
        float[] vals;
        if (jsonVal instanceof Number) {
            if (n != 1) {
                throw new RuntimeException("E501: param '" + pname
                        + "': model input expects " + n
                        + " values, got 1");
            }
            vals = new float[]{((Number) jsonVal).floatValue()};
        } else if (jsonVal instanceof JSONArray) {
            JSONArray arr = (JSONArray) jsonVal;
            if (arr.length() != n) {
                throw new RuntimeException("E501: param '" + pname
                        + "': model input expects " + n + " values, got "
                        + arr.length());
            }
            vals = new float[n];
            for (int i = 0; i < n; i++) {
                Object e = arr.opt(i);
                if (!(e instanceof Number)) {
                    throw new RuntimeException("E502: param '" + pname
                            + "': [Float] value required");
                }
                vals[i] = ((Number) e).floatValue();
            }
        } else {
            throw new RuntimeException("E502: param '" + pname
                    + "': [Float] value required");
        }
        ByteBuffer bb = ByteBuffer.allocateDirect(n * 4)
                .order(ByteOrder.nativeOrder());
        for (float v : vals) bb.putFloat(v);
        bb.rewind();
        return bb;
    }

    private static Object emptyOutput(Tensor t) {
        if (t.dataType() == DataType.STRING) {
            // Scalar string output: the interpreter fills in element 0.
            return new byte[1][];
        }
        int n = numElements(t.shape());
        return ByteBuffer.allocateDirect(Math.max(n, 1) * 4)
                .order(ByteOrder.nativeOrder());
    }

    private static String outputJson(Tensor t, Object outBuf) {
        if (t.dataType() == DataType.STRING) {
            byte[][] arr = (byte[][]) outBuf;
            byte[] b = arr.length > 0 && arr[0] != null ? arr[0]
                    : new byte[0];
            return quote(new String(b, StandardCharsets.UTF_8));
        }
        if (t.dataType() != DataType.FLOAT32) {
            throw new RuntimeException(
                    "E502: model output is not float/string (v1)");
        }
        ByteBuffer bb = (ByteBuffer) outBuf;
        bb.rewind();
        int n = numElements(t.shape());
        int[] shape = t.shape();
        boolean scalar = shape.length == 0
                || (shape.length == 1 && shape[0] == 1);
        if (scalar) {
            return Float.toString(bb.getFloat());
        }
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < n; i++) {
            if (i > 0) sb.append(',');
            sb.append(Float.toString(bb.getFloat()));
        }
        return sb.append(']').toString();
    }

    private static String quote(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.append('"').toString();
    }
}
