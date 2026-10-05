package com.misa.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.provider.MediaStore;
import android.widget.EditText;

/** Host Activity for a Misa Android app (Phase 4, M6).
 *
 * Owns one MisaWidgets (one MisaUI) and the "Misa" HandlerThread.
 * Every native entry — boot, event dispatch, camera result — is
 * posted to the Misa thread; the UI thread only ever touches Views.
 *
 * P3 (provider keys etc.): supplied per launch, never baked in —
 * intent extras (e.g. via adb: am start ... --es OPENAI_KEY sk-...)
 * are pushed to nativeSetConfig before boot. The demo ships with an
 * empty config, so network model calls degrade to the catchable E401
 * until keys are supplied.
 */
public class MisaActivity extends Activity {
    private static final int REQ_CAMERA = 1001;

    private MisaWidgets widgets;
    private HandlerThread misaThread;
    private Handler misaHandler;

    private Uri pendingPhotoUri;
    private int pendingPhotoHandler;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        misaThread = new HandlerThread("Misa");
        misaThread.start();
        misaHandler = new Handler(misaThread.getLooper());

        // No system action bar: Misa screens draw their own headings.
        // (Without this, the bar overlaps the first screen's top.)
        android.app.ActionBar ab = getActionBar();
        if (ab != null) ab.hide();

        widgets = new MisaWidgets(this, misaHandler);
        setContentView(widgets.rootView());

        final Bundle extras = getIntent() != null
                ? getIntent().getExtras() : null;
        final String filesDir =
                getFilesDir().getAbsolutePath();
        misaHandler.post(() -> {
            boolean hasMock = installMockAssets(filesDir);
            if (!hasMock) {
                // Live-model build: need the user's API key before boot.
                // Stored in SharedPreferences on first launch; the key
                // never leaves this phone and is never baked into the APK.
                awaitModelKey();
            }
            if (extras != null) {
                for (String k : extras.keySet()) {
                    Object v = extras.get(k);
                    if (v != null) {
                        MisaNative.nativeSetConfig(k,
                                String.valueOf(v));
                    }
                }
            }
            MisaNative.nativeBoot(widgets, filesDir);
        });
    }

    /** Stage bundled *.mock.json assets into filesDir and point
     * MISA_MOCK_FILE at the first one, so demo apps with scripted
     * model responses work with no keys and no network.
     * Returns true when a mock asset was staged. */
    private boolean installMockAssets(String filesDir) {
        try {
            String[] names = getAssets().list("");
            if (names == null) return false;
            for (String name : names) {
                if (!name.endsWith(".mock.json")) continue;
                java.io.File out = new java.io.File(filesDir, name);
                try (java.io.InputStream in = getAssets().open(name);
                     java.io.OutputStream os =
                             new java.io.FileOutputStream(out)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
                }
                MisaNative.nativeSetConfig("MISA_MOCK_FILE",
                        out.getAbsolutePath());
                android.util.Log.i("MISA",
                        "staged mock assets: " + name);
                return true; // first mock wins
            }
        } catch (java.io.IOException e) {
            android.util.Log.w("MISA",
                    "mock asset staging failed: " + e.getMessage());
        }
        return false;
    }

    /** Live-model builds only: ensure a working model API key exists,
     * asking once on first launch (and re-asking if hcnsec rejects the
     * saved one). Runs on the Misa thread; dialogs show on the UI
     * thread and this method blocks until answered. The key is
     * verified with a cheap GET /v1/models before boot, so a bad key
     * surfaces here as "rejected" instead of a cryptic E401 later. */
    private void awaitModelKey() {
        SharedPreferences prefs =
                getSharedPreferences("misa", MODE_PRIVATE);
        String key = prefs.getString("MISA_MODEL_KEY", null);
        String errNote = null;
        for (;;) {
            if (key == null || key.isEmpty()) {
                key = promptForKey(errNote);
                errNote = null;
                continue;
            }
            int code = testKeyOnce(key);
            if (code == 200) break;               // verified live
            if (code == 401 || code == 403) {     // definitive rejection
                errNote = "hcnsec rejected that key (HTTP " + code + "). "
                        + "Paste a fresh key from your hcnsec dashboard.";
                key = null;
                continue;
            }
            break;  // network hiccup / server error: proceed, the
                    // diagnose call will report the real problem
        }
        prefs.edit().putString("MISA_MODEL_KEY", key).apply();
        applyModelKey(key);
    }

    /** Cheap key check: GET /v1/models. Returns the HTTP status,
     * or -1 when the network itself failed. */
    private int testKeyOnce(String key) {
        java.net.HttpURLConnection conn = null;
        try {
            java.net.URL url =
                    new java.net.URL("https://api.hcnsec.cn/v1/models");
            conn = (java.net.HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setRequestProperty("Authorization", "Bearer " + key);
            conn.setConnectTimeout(15000);
            conn.setReadTimeout(15000);
            return conn.getResponseCode();
        } catch (Exception e) {
            return -1;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** Show the key-entry dialog (on the UI thread) and block the
     * Misa thread until the user saves. errNote, when non-null, is
     * shown above the field. */
    private String promptForKey(String errNote) {
        final java.util.concurrent.CountDownLatch latch =
                new java.util.concurrent.CountDownLatch(1);
        final String[] out = new String[1];
        runOnUiThread(() -> {
            final EditText input = new EditText(MisaActivity.this);
            input.setHint("paste API key");
            input.setSingleLine(true);
            String msg = "PlantDoc Live calls a real AI model. "
                    + "Paste your hcnsec API key — it is stored "
                    + "only on this phone, never sent anywhere "
                    + "except the model API.";
            if (errNote != null) msg = errNote + "\n\n" + msg;
            new AlertDialog.Builder(MisaActivity.this)
                    .setTitle("Model API key")
                    .setMessage(msg)
                    .setView(input)
                    .setPositiveButton("Save", (d, w) -> {
                        out[0] = input.getText().toString().trim();
                        latch.countDown();
                    })
                    .setCancelable(false)
                    .show();
        });
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return out[0] != null ? out[0] : "";
    }

    private void applyModelKey(String key) {
        MisaNative.nativeSetConfig("MISA_MODEL_KEY", key);
        MisaNative.nativeSetConfig("MISA_OPENAI_BASE_URL",
                "https://api.hcnsec.cn/v1");
    }

    /** CameraButton: capture into the MediaStore, hand the content://
     * URI to the onCapture handler. No androidx dependency — plain
     * ACTION_IMAGE_CAPTURE. Must be called on the UI thread (it is:
     * click listeners run there). */
    public void takePicture(int handlerId) {
        ContentValues cv = new ContentValues();
        cv.put(MediaStore.Images.Media.DISPLAY_NAME,
                "misa_" + System.currentTimeMillis() + ".jpg");
        cv.put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
        Uri uri = getContentResolver().insert(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv);
        if (uri == null) return;
        pendingPhotoUri = uri;
        pendingPhotoHandler = handlerId;
        Intent i = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        i.putExtra(MediaStore.EXTRA_OUTPUT, uri);
        if (i.resolveActivity(getPackageManager()) != null) {
            startActivityForResult(i, REQ_CAMERA);
        } else {
            pendingPhotoUri = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode,
                                    Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_CAMERA && resultCode == RESULT_OK
                && pendingPhotoUri != null) {
            final String uri = pendingPhotoUri.toString();
            final int handlerId = pendingPhotoHandler;
            pendingPhotoUri = null;
            misaHandler.post(() -> MisaNative.nativePhotoResult(uri,
                    handlerId));
        } else {
            pendingPhotoUri = null;
        }
    }

    @Override
    protected void onDestroy() {
        misaThread.quitSafely();
        super.onDestroy();
    }
}
