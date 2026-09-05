package com.mimo2api.app;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.TextView;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

public class MainActivity extends AppCompatActivity {

    private WebView web;
    private TextView status;
    private Button loginBtn;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private static final String BASE = "http://127.0.0.1:" + ServerService.PORT;
    private static final String ADMIN_USER = "admin";
    private static final int REQ_LOGIN = 2001;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web);
        status = findViewById(R.id.status);
        loginBtn = findViewById(R.id.btn_login);
        loginBtn.setOnClickListener(v -> {
            // 打开内置浏览器：保留登录态，可在网页内自行退出/切换账号
            startActivityForResult(new Intent(this, LoginActivity.class), REQ_LOGIN);
        });

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        // 关键：不设 WebChromeClient 时 JS 的 confirm() 恒返回 false，
        // 管理页"删除账号"等带确认框的操作会静默失效
        web.setWebChromeClient(new android.webkit.WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView v, String url, String msg,
                                     android.webkit.JsResult result) {
                new androidx.appcompat.app.AlertDialog.Builder(MainActivity.this)
                        .setMessage(msg)
                        .setPositiveButton(getString(R.string.ok), (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView v, String url, String msg,
                                       android.webkit.JsResult result) {
                new androidx.appcompat.app.AlertDialog.Builder(MainActivity.this)
                        .setMessage(msg)
                        .setPositiveButton(getString(R.string.ok), (d, w) -> result.confirm())
                        .setNegativeButton(getString(R.string.cancel), (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsPrompt(WebView v, String url, String msg, String defaultValue,
                                      android.webkit.JsPromptResult result) {
                final android.widget.EditText et = new android.widget.EditText(MainActivity.this);
                et.setText(defaultValue);
                new androidx.appcompat.app.AlertDialog.Builder(MainActivity.this)
                        .setMessage(msg)
                        .setView(et)
                        .setPositiveButton(getString(R.string.ok), (d, w) -> result.confirm(et.getText().toString()))
                        .setNegativeButton(getString(R.string.cancel), (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return false;
            }
            @Override
            public void onReceivedHttpAuthRequest(WebView v,
                                                  android.webkit.HttpAuthHandler handler,
                                                  String host, String realm) {
                handler.proceed(ADMIN_USER, AdminCreds.password(MainActivity.this));
            }
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req != null && req.isForMainFrame()) {
                    ui.postDelayed(MainActivity.this::loadAdmin, 1500);
                }
            }
        });

        requestNotifPermission();

        Intent svc = new Intent(this, ServerService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }

        waitForServer(0);
    }

    private void requestNotifPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1001);
        }
    }

    /** 等 uvicorn 就绪；就绪后按"有无账号"决定进登录页还是管理页。 */
    private void waitForServer(final int attempt) {
        if (attempt > 240) {
            status.setText(getString(R.string.status_timeout));
            return;
        }
        new Thread(() -> {
            boolean ok = ping();
            if (ok) {
                // 无论有无账号都直接进管理后台；
                // 登录入口在右下角"登录小米账号"按钮，不再自动跳转登录页
                ui.post(() -> showAdmin());
            } else {
                ui.post(() -> {
                    status.setText(getString(R.string.status_starting) + "\n" + (attempt / 2) + "s");
                    ui.postDelayed(() -> waitForServer(attempt + 1), 500);
                });
            }
        }).start();
    }

    private void showAdmin() {
        status.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        loginBtn.setVisibility(View.VISIBLE);
        loadAdmin();
    }

    /** 带 Basic 认证头加载管理页。 */
    private void loadAdmin() {
        java.util.Map<String, String> h = new java.util.HashMap<>();
        h.put("Authorization", basic());
        web.loadUrl(BASE + "/", h);
    }

    private String basic() {
        String cred = ADMIN_USER + ":" + AdminCreds.password(this);
        return "Basic " + android.util.Base64.encodeToString(
                cred.getBytes(StandardCharsets.UTF_8), android.util.Base64.NO_WRAP);
    }

    /** 查询本地服务是否已有可用账号。 */
    private boolean hasAccount() {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(BASE + "/api/accounts").openConnection();
            c.setConnectTimeout(4000);
            c.setReadTimeout(6000);
            c.setRequestProperty("Authorization", basic());
            InputStream in = c.getInputStream();
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            in.close();
            JSONObject j = new JSONObject(bo.toString("UTF-8"));
            JSONArray arr = j.optJSONArray("accounts");
            return arr != null && arr.length() > 0;
        } catch (Throwable t) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private boolean ping() {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(BASE + "/").openConnection();
            c.setConnectTimeout(800);
            c.setReadTimeout(800);
            c.setRequestMethod("GET");
            return c.getResponseCode() > 0;   // 401 也表示已就绪
        } catch (IOException e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req == REQ_LOGIN) {
            // 无论导入成功与否都进管理页（失败时用户可在页内手动处理）
            showAdmin();
        }
    }

    @Override
    public void onRequestPermissionsResult(int req, @NonNull String[] p, @NonNull int[] g) {
        super.onRequestPermissionsResult(req, p, g);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.getVisibility() == View.VISIBLE && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);
    }
}
