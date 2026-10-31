package io.github.marselnenaj.tomacco.unicodefixture;
import android.app.Activity;
import android.os.Bundle;
import android.os.SystemClock;
import android.widget.*;
public class MainActivity extends Activity {
    private double work;
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);root.setPadding(32,120,32,80);
        TextView title=new TextView(this);title.setText("Tomacco · Native input qualification");root.addView(title);
        EditText input=new EditText(this);input.setId(android.R.id.edit);input.setContentDescription("Unicode input");input.setHint("Text, Umlaute, Emojis");root.addView(input);
        Button burn=new Button(this);burn.setText("CPU WORK");root.addView(burn);
        burn.setOnClickListener(view->{
            long deadline=SystemClock.uptimeMillis()+1500;
            while(SystemClock.uptimeMillis()<deadline)for(int i=1;i<10000;i++)work+=Math.sqrt(i+work%10);
            burn.setText("WORK COMPLETE");
        });
        setContentView(root);
    }
}
