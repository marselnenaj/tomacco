package __BUNDLE__;
import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.widget.*;
public class MainActivity extends Activity {
    static { System.loadLibrary("TomaccoCore"); }
    private static native int nextCount(int current);
    private int count;
    public void onCreate(Bundle state) {
        super.onCreate(state);
        count = getPreferences(0).getInt("count", 0);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setGravity(17); layout.setPadding(48, 80, 48, 80);
        layout.setBackgroundColor(Color.rgb(17, 22, 35));
        TextView title = new TextView(this);
        title.setText("__NAME__"); title.setTextSize(32); title.setTextColor(Color.WHITE);
        layout.addView(title);
        TextView value = new TextView(this);
        value.setTextSize(64); value.setTextColor(Color.rgb(163, 195, 255));
        value.setText(String.valueOf(count)); layout.addView(value);
        Button add = new Button(this); add.setText("Add one");
        add.setOnClickListener(v -> { count = nextCount(count); value.setText(String.valueOf(count)); getPreferences(0).edit().putInt("count", count).apply(); });
        layout.addView(add);
        Button reset = new Button(this); reset.setText("Reset");
        reset.setOnClickListener(v -> { count=0; value.setText("0"); getPreferences(0).edit().putInt("count", 0).apply(); });
        layout.addView(reset); setContentView(layout);
        layout.setOnApplyWindowInsetsListener((v, insets) -> { v.setPadding(48 + insets.getSystemWindowInsetLeft(), 32 + insets.getSystemWindowInsetTop(), 48 + insets.getSystemWindowInsetRight(), 32 + insets.getSystemWindowInsetBottom()); return insets; });
    }
}
