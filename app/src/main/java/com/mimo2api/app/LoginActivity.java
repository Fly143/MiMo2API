package com.mimo2api.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 内置浏览器：登录小米 MiMo 网页版，自动抓取
 * serviceToken / userId / xiaomichatbot_ph 三个 Cookie 并导入本地服务。
 */
public class LoginActivity extends AppCompatActivity {

    private static final String TAG = "MiMo2API";
    private static final String MIMO_URL = "https://aistudio.xiaomimimo.com/";
    private static final String BASE = "http://127.0.0.1:" + ServerService.PORT;

    private WebView web;
    private TextView hint;
    private Button importBtn;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean imported = false;
    private volatile boolean importing = false;
    private Runnable poller;
    private int lastRawLen = -1;
    private static final String[] COOKIE_DOMAINS = {
            "https://aistudio.xiaomimimo.com/",
            "https://xiaomimimo.com/",
            "https://account.xiaomi.com/",
            "https://mi.com/",
    };

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        setContentView(R.layout.activity_login);

        web = findViewById(R.id.login_web);
        hint = findViewById(R.id.login_hint);
        importBtn = findViewById(R.id.btn_import);

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(web, true);

        // 默认保留登录态：用户可在网页内自行退出/切换账号

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        // 用移动端 UA，走小米的手机登录流程
        s.setUserAgentString("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
                + "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36");

        web.setWebChromeClient(new android.webkit.WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView v, String url, String msg,
                                     android.webkit.JsResult result) {
                new androidx.appcompat.app.AlertDialog.Builder(LoginActivity.this)
                        .setMessage(msg)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView v, String url, String msg,
                                       android.webkit.JsResult result) {
                new androidx.appcompat.app.AlertDialog.Builder(LoginActivity.this)
                        .setMessage(msg)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setNegativeButton("取消", (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return false;   // 站内跳转（含账号登录页）都留在本 WebView
            }

            @Override
            public void onPageStarted(WebView v, String url, Bitmap f) {
                super.onPageStarted(v, url, f);
                checkCookies(false);
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                super.onPageFinished(v, url);
                CookieManager.getInstance().flush();
                checkCookies(false);
            }
        });

        importBtn.setOnClickListener(v -> checkCookies(true));
        findViewById(R.id.btn_reload).setOnClickListener(v -> web.loadUrl(MIMO_URL));

        // 需要彻底换账号时才清（网页内退不干净的兜底）
        findViewById(R.id.btn_clear).setOnClickListener(v ->
            new androidx.appcompat.app.AlertDialog.Builder(this)
                .setTitle("清除登录态")
                .setMessage("将清空浏览器 Cookie 与缓存，需重新登录小米账号。\n\n（一般情况下可直接在网页内退出登录，无需此操作）")
                .setPositiveButton("清除", (d, w) -> {
                    CookieManager c2 = CookieManager.getInstance();
                    c2.removeAllCookies(ok -> c2.flush());
                    web.clearCache(true);
                    web.clearHistory();
                    android.webkit.WebStorage.getInstance().deleteAllData();
                    imported = false;
                    lastRawLen = -1;
                    web.loadUrl(MIMO_URL);
                    Toast.makeText(this, "已清除，请重新登录", Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton("取消", null)
                .show());

        web.loadUrl(MIMO_URL);

        // 登录常由页面内 JS 跳转完成，不触发 onPageFinished，故定时轮询
        poller = new Runnable() {
            @Override public void run() {
                checkCookies(false);
                if (!imported) ui.postDelayed(this, 1500);
            }
        };
        ui.postDelayed(poller, 2000);
    }

    /** 读取 cookie；manual=true 时即使不完整也提示用户。 */
    private void checkCookies(boolean manual) {
        CookieManager cmg = CookieManager.getInstance();
        cmg.flush();
        StringBuilder sb = new StringBuilder();
        for (String d : COOKIE_DOMAINS) {
            String c = cmg.getCookie(d);
            if (c != null && !c.isEmpty()) sb.append(c).append("; ");
        }
        String raw = sb.toString();

        // 实测小米下发的是 xiaomichatbot_serviceToken（带前缀），兼容无前缀写法
        String st = pick(raw, "xiaomichatbot_serviceToken");
        if (st.isEmpty()) st = pick(raw, "serviceToken");
        String uid = pick(raw, "userId");
        String ph = pick(raw, "xiaomichatbot_ph");

        // 诊断：打印所有 cookie 名，便于定位实际字段
        if (raw.length() != lastRawLen) {
            lastRawLen = raw.length();
            StringBuilder names = new StringBuilder();
            for (String kv : raw.split(";")) {
                int eq = kv.indexOf('=');
                if (eq > 0) names.append(kv.substring(0, eq).trim()).append(",");
            }
            Log.i(TAG, "cookie names = " + names);
        }

        boolean full = !st.isEmpty() && !uid.isEmpty() && !ph.isEmpty();

        if (full) {
            importBtn.setEnabled(true);
            importBtn.setText("导入账号 (userId " + uid + ")");
            if (!imported && !importing) {
                // 凭据齐全 -> 自动导入，无需用户操作
                hint.setText("✅ 已捕获登录凭据，正在自动导入…");
                doImport(st, uid, ph);
            } else if (manual) {
                doImport(st, uid, ph);
            }
        } else {
            importBtn.setEnabled(false);
            importBtn.setText("等待登录…");
            hint.setText("请在下方登录小米账号（已捕获 "
                    + (st.isEmpty() ? "" : "serviceToken ")
                    + (uid.isEmpty() ? "" : "userId ")
                    + (ph.isEmpty() ? "" : "xiaomichatbot_ph ")
                    + "）");
            if (manual) {
                Toast.makeText(this, "凭据尚未齐全，请先完成登录并进入对话页", Toast.LENGTH_SHORT).show();
            }
        }
    }

    private static String pick(String cookies, String name) {
        Matcher m = Pattern.compile("(?:^|;\\s*)" + Pattern.quote(name) + "=\"?([^\";]+)")
                .matcher(cookies);
        return m.find() ? m.group(1).trim() : "";
    }

    /** POST 到本地服务已有的 /api/account/import-cookie（服务端会真实校验）。 */
    private void doImport(String st, String uid, String ph) {
        if (importing) return;
        importing = true;
        hint.setText("正在验证并导入…（首次校验需连一次上游）");
        importBtn.setEnabled(false);

        new Thread(() -> {
            String msg;
            boolean ok = false;
            HttpURLConnection c = null;
            try {
                JSONObject body = new JSONObject();
                body.put("serviceToken", st);
                body.put("userId", uid);
                body.put("xiaomichatbot_ph", ph);

                c = (HttpURLConnection) new URL(BASE + "/api/account/import-cookie").openConnection();
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setConnectTimeout(10000);
                c.setReadTimeout(60000);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                String cred = "admin:" + AdminCreds.password(this);
                c.setRequestProperty("Authorization", "Basic " + android.util.Base64.encodeToString(
                        cred.getBytes(StandardCharsets.UTF_8), android.util.Base64.NO_WRAP));

                OutputStream os = c.getOutputStream();
                os.write(body.toString().getBytes(StandardCharsets.UTF_8));
                os.close();

                int code = c.getResponseCode();
                java.io.InputStream in = (code >= 400) ? c.getErrorStream() : c.getInputStream();
                java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[4096];
                int n;
                while (in != null && (n = in.read(buf)) > 0) bo.write(buf, 0, n);
                String resp = bo.toString("UTF-8");
                Log.i(TAG, "import-cookie " + code + " " + resp);

                JSONObject j = new JSONObject(resp);
                ok = j.optBoolean("ok", false);
                msg = ok ? ("✅ 导入成功，账号 " + j.optString("user_id", uid))
                         : ("❌ " + j.optString("error", "导入失败 HTTP " + code));
            } catch (Throwable t) {
                msg = "❌ 导入异常: " + t.getMessage();
                Log.e(TAG, "import failed", t);
            } finally {
                if (c != null) c.disconnect();
            }

            final String fm = msg;
            final boolean fok = ok;
            importing = false;
            ui.post(() -> {
                hint.setText(fm);
                importBtn.setEnabled(true);
                importBtn.setText(fok ? "已导入 · 返回" : "重试导入");
                if (fok) {
                    imported = true;
                    Toast.makeText(this, "账号导入成功", Toast.LENGTH_SHORT).show();
                    setResult(Activity.RESULT_OK);
                    ui.postDelayed(this::finish, 1200);
                }
            });
        }, "mimo-import").start();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
