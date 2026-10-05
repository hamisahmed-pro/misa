package com.misa.app;

import android.app.Activity;
import android.content.ContentResolver;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.os.Handler;
import android.os.Looper;
import android.util.SparseArray;
import android.util.TypedValue;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.InputStream;
import java.util.concurrent.CountDownLatch;

/** The Java side of the Misa Android Views driver (rt/ui_android.c).
 *
 * Owns every View the Misa program builds. Called from C through JNI;
 * view work always runs on the UI thread (runOnUiThread + latch when
 * a result is needed; direct when already on the UI thread, so
 * handler-driven rebuilds never deadlock).
 *
 * Widget ids are 1-based ints handed out in creation order; the C
 * driver stores the mapping handle -> id.
 */
public class MisaWidgets {
    private final MisaActivity activity;
    private final Handler misaHandler;

    private final FrameLayout root;      // set as the content view
    private final ScrollView scroll;     // visible scroll container
    private final LinearLayout container; // current screen lives here
    private final FrameLayout staging;   // invisible; in-progress trees

    private final SparseArray<View> views = new SparseArray<>();
    private int nextId = 1;

    public MisaWidgets(MisaActivity activity, Handler misaHandler) {
        this.activity = activity;
        this.misaHandler = misaHandler;

        root = new FrameLayout(activity);
        scroll = new ScrollView(activity);
        container = new LinearLayout(activity);
        container.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        container.setPadding(pad, pad, pad, pad);
        scroll.addView(container,
                new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.WRAP_CONTENT));
        staging = new FrameLayout(activity);
        staging.setVisibility(View.GONE);
        root.addView(scroll,
                new FrameLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT,
                        ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(staging,
                new FrameLayout.LayoutParams(0, 0));
    }

    public View rootView() {
        return root;
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP, v,
                activity.getResources().getDisplayMetrics());
    }

    /** Run on the UI thread, blocking the Misa thread for completion. */
    private void onUi(Runnable r) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            r.run();
            return;
        }
        final CountDownLatch latch = new CountDownLatch(1);
        final Throwable[] err = new Throwable[1];
        activity.runOnUiThread(() -> {
            try {
                r.run();
            } catch (Throwable t) {
                err[0] = t;
            } finally {
                latch.countDown();
            }
        });
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        if (err[0] != null) throw new RuntimeException(err[0]);
    }

    /** Fire-and-forget onto the UI thread. */
    private void onUiAsync(Runnable r) {
        activity.runOnUiThread(r);
    }

    private View find(int id) {
        return views.get(id);
    }

    // ------------------------------------------------------------------
    // tree construction (called from rt/ui_android.c via JNI)
    // ------------------------------------------------------------------

    /** Create a view of the given Misa component kind, attach it to
     * parentId (0 = staging area), return the view id. */
    public int create(String kind, int parentId) {
        final int[] out = new int[1];
        onUi(() -> {
            View v = makeView(kind);
            int id = nextId++;
            views.put(id, v);
            ViewGroup parent = parentId > 0
                    ? (ViewGroup) find(parentId) : staging;
            if (parent != null) {
                parent.addView(v, defaultParams(v));
            }
            out[0] = id;
        });
        return out[0];
    }

    private ViewGroup.LayoutParams defaultParams(View v) {
        if (v.getParent() instanceof LinearLayout) {
            return new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
        }
        return new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private View makeView(String kind) {
        switch (kind) {
            case "__screen__":
            case "Column": {
                LinearLayout l = new LinearLayout(activity);
                l.setOrientation(LinearLayout.VERTICAL);
                return l;
            }
            case "Row": {
                LinearLayout l = new LinearLayout(activity);
                l.setOrientation(LinearLayout.HORIZONTAL);
                return l;
            }
            case "Spacer": {
                View s = new View(activity);
                LinearLayout.LayoutParams p =
                        new LinearLayout.LayoutParams(0, 0, 1f);
                s.setLayoutParams(p);
                return s;
            }
            case "Heading": {
                TextView t = new TextView(activity);
                t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 24);
                t.setTypeface(t.getTypeface(),
                        android.graphics.Typeface.BOLD);
                t.setPadding(0, dp(8), 0, dp(8));
                return t;
            }
            case "Banner": {
                TextView t = new TextView(activity);
                t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
                t.setPadding(dp(12), dp(10), dp(12), dp(10));
                return t;
            }
            case "Text":
                return new TextView(activity);
            case "Button":
                return new Button(activity);
            case "TextInput": {
                EditText e = new EditText(activity);
                e.setSingleLine(true);
                return e;
            }
            case "CameraButton": {
                Button b = new Button(activity);
                b.setText("Take photo");
                return b;
            }
            case "Image": {
                ImageView iv = new ImageView(activity);
                iv.setAdjustViewBounds(true);
                return iv;
            }
            case "List": {
                // M6 slice: a vertical LinearLayout of TextView rows.
                // (Doc §4.4 names RecyclerView; the single-text-row
                // shape is identical and recycling is a later
                // optimization — see docs/android.md.)
                LinearLayout l = new LinearLayout(activity);
                l.setOrientation(LinearLayout.VERTICAL);
                return l;
            }
            default: {
                TextView t = new TextView(activity);
                t.setText("[unknown component: " + kind + "]");
                return t;
            }
        }
    }

    public void setText(int id, String text) {
        onUi(() -> {
            View v = find(id);
            if (v instanceof TextView) ((TextView) v).setText(text);
        });
    }

    public void setHint(int id, String hint) {
        onUi(() -> {
            View v = find(id);
            if (v instanceof EditText) ((EditText) v).setHint(hint);
        });
    }

    public void setBannerStyle(int id, String style) {
        onUi(() -> {
            View v = find(id);
            if (!(v instanceof TextView)) return;
            int bg;
            switch (style) {
                case "warning": bg = 0xFFFFF3CD; break; // amber-100
                case "error":   bg = 0xFFF8D7DA; break; // red-100
                case "info":
                default:        bg = 0xFFD1ECF1; break; // blue-100
            }
            v.setBackgroundColor(bg);
            ((TextView) v).setTextColor(Color.parseColor("#212529"));
        });
    }

    public void setImage(int id, String pathOrUri) {
        onUi(() -> {
            View v = find(id);
            if (!(v instanceof ImageView) || pathOrUri == null) return;
            try {
                Bitmap bmp;
                if (pathOrUri.startsWith("content://")) {
                    ContentResolver cr = activity.getContentResolver();
                    try (InputStream in = cr.openInputStream(
                            android.net.Uri.parse(pathOrUri))) {
                        bmp = BitmapFactory.decodeStream(in);
                    }
                } else {
                    BitmapFactory.Options o =
                            new BitmapFactory.Options();
                    o.inSampleSize = 2; // camera photos are huge
                    bmp = BitmapFactory.decodeFile(pathOrUri, o);
                }
                if (bmp != null) ((ImageView) v).setImageBitmap(bmp);
            } catch (Exception e) {
                android.util.Log.w("MISA",
                        "setImage failed: " + e.getMessage());
            }
        });
    }

    public void listItem(int id, String text) {
        onUi(() -> {
            View v = find(id);
            if (v instanceof LinearLayout) {
                TextView t = new TextView(activity);
                t.setText("\u2022 " + text);
                t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
                ((LinearLayout) v).addView(t,
                        new LinearLayout.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.WRAP_CONTENT));
            }
        });
    }

    public void setHandler(int id, String event, int handlerId) {
        onUi(() -> {
            View v = find(id);
            if (!(v instanceof Button)) return;
            if ("onTap".equals(event)) {
                ((Button) v).setOnClickListener(b ->
                        misaHandler.post(() ->
                                MisaNative.nativeDispatch(handlerId)));
            } else if ("onCapture".equals(event)) {
                ((Button) v).setOnClickListener(b ->
                        activity.takePicture(handlerId));
            }
        });
    }

    /** Show the built screen: move rootId's tree from staging into
     * the visible container. */
    public void show(int id) {
        onUi(() -> {
            View v = find(id);
            if (v == null) return;
            container.removeAllViews();
            if (v.getParent() instanceof ViewGroup) {
                ((ViewGroup) v.getParent()).removeView(v);
            }
            container.addView(v,
                    new LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.MATCH_PARENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT));
            scroll.scrollTo(0, 0);
        });
    }

    public String getText(int id) {
        final String[] out = new String[1];
        onUi(() -> {
            View v = find(id);
            out[0] = (v instanceof EditText)
                    ? ((EditText) v).getText().toString() : "";
        });
        return out[0] != null ? out[0] : "";
    }

    public void reportError(String text) {
        onUiAsync(() -> {
            android.util.Log.e("MISA", text);
            Toast.makeText(activity, text,
                    Toast.LENGTH_LONG).show();
        });
    }

    /** P2: log + toast, then finish the activity. Called on the Misa
     * thread; the native side parks the thread afterwards (never
     * exit() on mobile). */
    public void reportFatal(String text) {
        onUiAsync(() -> {
            android.util.Log.e("MISA", "FATAL: " + text);
            Toast.makeText(activity, "Misa fatal: " + text,
                    Toast.LENGTH_LONG).show();
            activity.finish();
        });
    }
}
