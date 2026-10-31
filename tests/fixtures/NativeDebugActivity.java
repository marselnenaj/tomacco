package io.github.marselnenaj.tomacco.nativedebugfixture;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

public class MainActivity extends Activity {
    static { System.loadLibrary("tomacconative"); }
    private static native int calculate(int seed);
    private volatile boolean running = true;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        TextView output = new TextView(this);
        output.setTextSize(28);
        output.setPadding(24, 80, 24, 24);
        output.setText("Native calculation starting");
        setContentView(output);
        new Thread(() -> {
            while (running) {
                int value = calculate(40);
                runOnUiThread(() -> output.setText("Native value: " + value));
                try { Thread.sleep(300); } catch (InterruptedException ignored) { break; }
            }
        }, "Tomacco Native Counter").start();
    }
    @Override public void onDestroy() { running = false; super.onDestroy(); }
}
