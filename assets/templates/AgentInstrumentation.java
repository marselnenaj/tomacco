package io.github.marselnenaj.tomacco.automation;

import android.app.Instrumentation;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.os.Bundle;
import android.os.SystemClock;
import android.os.Build;
import android.util.Base64;
import android.view.accessibility.AccessibilityNodeInfo;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;

/** Runs only when explicitly invoked by an authorized adb instrumentation session. */
public final class AgentInstrumentation extends Instrumentation {
    private Bundle arguments;
    @Override public void onCreate(Bundle args) { super.onCreate(args); arguments=args; start(); }
    private static String text(CharSequence value) { return value==null ? "" : value.toString(); }
    private static String decode(String value) { return new String(Base64.decode(value,Base64.DEFAULT),StandardCharsets.UTF_8); }
    private static boolean matches(AccessibilityNodeInfo node, JSONObject selector) throws Exception {
        for(Iterator<String> keys=selector.keys();keys.hasNext();) {
            String key=keys.next(); String actual;
            switch(key) {
                case "index": continue;
                case "text": actual=text(node.getText()); break;
                case "text_contains": if(!text(node.getText()).contains(selector.getString(key)))return false; else continue;
                case "resource_id": actual=text(node.getViewIdResourceName()); break;
                case "description": actual=text(node.getContentDescription()); break;
                case "class": actual=text(node.getClassName()); break;
                case "package": actual=text(node.getPackageName()); break;
                case "clickable": actual=Boolean.toString(node.isClickable()); break;
                case "enabled": actual=Boolean.toString(node.isEnabled()); break;
                case "checked": actual=Boolean.toString(node.isChecked()); break;
                case "selected": actual=Boolean.toString(node.isSelected()); break;
                default: throw new IllegalArgumentException("Unknown selector property: "+key);
            }
            if(!actual.equals(String.valueOf(selector.get(key))))return false;
        }
        return node.isVisibleToUser();
    }
    private static void visit(AccessibilityNodeInfo node,JSONObject selector,ArrayList<AccessibilityNodeInfo> found,int[] count) throws Exception {
        if(node==null)return;
        if(++count[0]>10000)throw new IllegalStateException("Accessibility tree exceeds 10,000 nodes");
        if(matches(node,selector))found.add(node);
        for(int i=0;i<node.getChildCount();i++)visit(node.getChild(i),selector,found,count);
    }
    @Override public void onStart() {
        JSONObject result=new JSONObject(); int code=0;
        try {
            result.put("protocol",1);
            if("fill".equals(arguments.getString("action"))) {
                JSONObject selector=new JSONObject(decode(arguments.getString("selector")));
                if(selector.length()==0 || (selector.length()==1 && selector.has("index")))throw new IllegalArgumentException("A semantic selector is required");
                String value=decode(arguments.getString("text"));
                UiAutomation automation=getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
                AccessibilityServiceInfo info=automation.getServiceInfo();
                info.flags|=AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
                automation.setServiceInfo(info);
                try {automation.waitForIdle(100,2000);}catch(java.util.concurrent.TimeoutException ignored){}
                if(Build.VERSION.SDK_INT>=33)automation.clearCache();
                ArrayList<AccessibilityNodeInfo> found=new ArrayList<>();
                AccessibilityNodeInfo root=automation.getRootInActiveWindow();
                visit(root,selector,found,new int[]{0});
                int index=selector.optInt("index",0);
                if(found.isEmpty() || index>=found.size())throw new IllegalStateException("Element no longer exists in active window: "+(root==null?"unavailable":text(root.getPackageName())));
                if(!selector.has("index") && found.size()!=1)throw new IllegalStateException("Selector is ambiguous: "+found.size()+" elements");
                AccessibilityNodeInfo node=found.get(index);
                if(!node.isEnabled() || !node.isEditable())throw new IllegalStateException("Element is not an enabled editable field");
                Bundle data=new Bundle();data.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,value);
                node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
                if(!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT,data))throw new IllegalStateException("Application rejected text replacement");
                long deadline=SystemClock.uptimeMillis()+2000;
                boolean observed=false;
                do {
                    SystemClock.sleep(50);
                    if(node.refresh() && !node.isPassword() && (value.equals(text(node.getText())) || (value.isEmpty() && node.isShowingHintText()))) {observed=true;break;}
                    if(node.isPassword())break;
                } while(SystemClock.uptimeMillis()<deadline);
                // Password values are deliberately not exposed by accessibility.
                if(!observed && !node.isPassword())throw new IllegalStateException("Application did not retain the requested text");
                result.put("characters",value.codePointCount(0,value.length()));
                result.put("observed",observed);
            } else if(!"ping".equals(arguments.getString("action"))) {
                throw new IllegalArgumentException("Unknown instrumentation action");
            }
            result.put("ok",true);
        } catch(Exception error) {
            code=1;
            try {result.put("ok",false);result.put("error",text(error.getMessage()));}catch(Exception ignored){}
        }
        Bundle output=new Bundle();
        output.putString("tomacco_result",Base64.encodeToString(result.toString().getBytes(StandardCharsets.UTF_8),Base64.NO_WRAP));
        finish(code,output);
    }
}
