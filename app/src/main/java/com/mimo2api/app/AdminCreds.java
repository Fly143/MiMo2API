package com.mimo2api.app;

import android.content.Context;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.ByteArrayOutputStream;

/** 从应用私有目录的 config.json 读取管理员密码。 */
public class AdminCreds {

    private static final String DEFAULT_PASSWORD = "admin";

    public static String password(Context ctx) {
        try {
            File f = new File(ctx.getFilesDir(), "config.json");
            if (!f.exists()) return DEFAULT_PASSWORD;
            FileInputStream in = new FileInputStream(f);
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
            in.close();
            JSONObject o = new JSONObject(bo.toString("UTF-8"));
            String p = o.optString("admin_password", DEFAULT_PASSWORD);
            return p.isEmpty() ? DEFAULT_PASSWORD : p;
        } catch (Throwable t) {
            return DEFAULT_PASSWORD;
        }
    }
}
