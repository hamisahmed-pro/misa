package com.misa.app;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.provider.MediaStore;

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

        widgets = new MisaWidgets(this, misaHandler);
        setContentView(widgets.rootView());

        final Bundle extras = getIntent() != null
                ? getIntent().getExtras() : null;
        final String filesDir =
                getFilesDir().getAbsolutePath();
        misaHandler.post(() -> {
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
