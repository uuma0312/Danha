# danha_mobile.py — 단아 모바일 타겟 (57단계)
#
# danha compile --target ios <파일.dh>     → Xcode 프로젝트 구조 생성
# danha compile --target android <파일.dh> → Gradle 프로젝트 구조 생성
#
# 실제 빌드는 Xcode / Android SDK 를 사용하며, 이 모듈은 프로젝트 뼈대를 생성한다.

import os
import re
import shutil
import sys


def _write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _safe_ident(name):
    """Return a Java/C/Gradle friendly identifier derived from a file name."""
    safe = re.sub(r'[^A-Za-z0-9_]', '_', name)
    safe = re.sub(r'_+', '_', safe).strip('_')
    if not safe or safe[0].isdigit():
        safe = 'danha_' + safe
    return safe


# ===== iOS 타겟 =====

def build_ios(source_path, output_dir=None):
    """
    Danha 소스를 iOS Xcode 프로젝트로 변환한다.
    출력: <base>_ios/ 디렉토리에 Xcode 프로젝트 뼈대
    """
    base_name_raw = os.path.splitext(os.path.basename(source_path))[0]
    base_name = _safe_ident(base_name_raw)
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)),
                                  f'{base_name}_ios')

    os.makedirs(output_dir, exist_ok=True)

    # main.m — Objective-C 진입점 (단아 컴파일 결과를 링크)
    _write_file(os.path.join(output_dir, 'main.m'), f'''\
#import <UIKit/UIKit.h>

// iOS hosts the app with Objective-C `main`, so the mobile compiler path
// should provide `danha_main`. The weak fallback keeps the shell buildable.
__attribute__((weak)) int danha_main(void) {{ return 0; }}

int main(int argc, char * argv[]) {{
    return danha_main();
}}
''')

    # Info.plist
    _write_file(os.path.join(output_dir, 'Info.plist'), f'''\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.danha.{base_name}</string>
    <key>CFBundleName</key>
    <string>{base_name}</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>UIRequiresFullScreen</key>
    <true/>
</dict>
</plist>
''')

    # Makefile (간단한 clang 빌드)
    _write_file(os.path.join(output_dir, 'Makefile'), f'''\
APP = {base_name}
SRC = main.m
DANHA_OBJ = ../{base_name}.o

XCRUN = xcrun --sdk iphoneos
CC    = $(XCRUN) clang
ARCH  = -arch arm64
MIN   = -miphoneos-version-min=14.0
FLAGS = $(ARCH) $(MIN) -framework UIKit -framework Foundation

$(APP): $(SRC) $(DANHA_OBJ)
\t$(CC) $(FLAGS) $^ -o $@

clean:
\trm -f $(APP)

.PHONY: clean
''')

    # README
    _write_file(os.path.join(output_dir, 'README.md'), f'''\
# {base_name} — iOS 빌드

## 빌드 방법

1. 단아 소스를 LLVM IR로 컴파일:
   ```
   danha compile {base_name}.dh
   ```

2. iOS 빌드:
   ```
   cd {base_name}_ios
   make
   ```

## 요구 사항
- macOS + Xcode 명령줄 도구
- iOS 14.0 이상 타겟
''')

    return output_dir


# ===== Android 타겟 =====

def build_android(source_path, output_dir=None):
    """
    Danha 소스를 Android Gradle 프로젝트로 변환한다.
    출력: <base>_android/ 디렉토리에 Gradle 프로젝트 뼈대
    """
    base_name_raw = os.path.splitext(os.path.basename(source_path))[0]
    base_name = _safe_ident(base_name_raw)
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)),
                                  f'{base_name}_android')

    app_dir = os.path.join(output_dir, 'app', 'src', 'main')
    jni_dir = os.path.join(app_dir, 'cpp')
    java_dir = os.path.join(app_dir, 'java', 'com', 'danha', base_name)
    danha_src_dir = os.path.join(app_dir, 'danha')
    tools_dir = os.path.join(output_dir, 'tools')

    os.makedirs(jni_dir, exist_ok=True)
    os.makedirs(java_dir, exist_ok=True)
    os.makedirs(danha_src_dir, exist_ok=True)
    os.makedirs(tools_dir, exist_ok=True)
    shutil.copy2(source_path, os.path.join(danha_src_dir, 'main.dh'))

    # settings.gradle
    _write_file(os.path.join(output_dir, 'settings.gradle'), f'''\
rootProject.name = "{base_name}"
include ':app'
''')

    # gradle.properties
    _write_file(os.path.join(output_dir, 'gradle.properties'), '''\
android.overridePathCheck=true
''')

    # build.gradle (루트)
    _write_file(os.path.join(output_dir, 'build.gradle'), '''\
buildscript {
    repositories { google(); mavenCentral() }
    dependencies {
        classpath 'com.android.tools.build:gradle:8.0.0'
    }
}
allprojects { repositories { google(); mavenCentral() } }
''')

    # app/build.gradle
    _write_file(os.path.join(output_dir, 'app', 'build.gradle'), f'''\
plugins {{
    id 'com.android.application'
}}

android {{
    namespace "com.danha.{base_name}"
    compileSdk 34
    defaultConfig {{
        applicationId "com.danha.{base_name}"
        minSdk 21
        targetSdk 34
        versionCode 1
        versionName "1.0"
        externalNativeBuild {{ cmake {{ cppFlags "" }} }}
        ndk {{
            abiFilters 'arm64-v8a'
        }}
    }}
    externalNativeBuild {{
        cmake {{ path "src/main/cpp/CMakeLists.txt" }}
    }}
}}

def danhaHome = System.getenv("DANHA_HOME")
if (danhaHome != null && danhaHome.length() > 0) {{
    tasks.register("buildDanhaObject", Exec) {{
        workingDir project.rootDir
        environment "DANHA_HOME", danhaHome
        if (System.getProperty("os.name").toLowerCase().contains("windows")) {{
            commandLine "python", "tools\\\\build_android_object.py"
        }} else {{
            commandLine "python3", "tools/build_android_object.py"
        }}
    }}
    preBuild.dependsOn("buildDanhaObject")
}}
''')

    # CMakeLists.txt (JNI C 브리지)
    _write_file(os.path.join(jni_dir, 'CMakeLists.txt'), f'''\
cmake_minimum_required(VERSION 3.22)
project({base_name})

add_library({base_name} SHARED danha_bridge.c)
set(DANHA_OBJECT "${{CMAKE_CURRENT_SOURCE_DIR}}/danha_main.o")
if(EXISTS "${{DANHA_OBJECT}}")
    target_sources({base_name} PRIVATE "${{DANHA_OBJECT}}")
    target_compile_definitions({base_name} PRIVATE DANHA_HAS_OBJECT=1)
endif()
target_link_libraries({base_name} android log)
''')

    # JNI 브리지 C
    _write_file(os.path.join(jni_dir, 'danha_bridge.c'), f'''\
#include <jni.h>
#include <android/log.h>

#ifdef DANHA_HAS_OBJECT
extern int main(void);
#else
// The weak fallback lets the generated Android shell build before a real
// cross-compiled Danha object is dropped into this target.
__attribute__((weak)) int main(void) {{ return 0; }}
#endif

JNIEXPORT void JNICALL
Java_com_danha_{base_name}_MainActivity_runDanha(JNIEnv *env, jobject thiz) {{
    main();
}}
''')

    # Android object build helper. It uses Danha's LLVM backend with an
    # aarch64-linux-android target triple, then CMake links the object above.
    _write_file(os.path.join(tools_dir, 'build_android_object.py'), '''\
#!/usr/bin/env python3
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'app', 'src', 'main', 'danha', 'main.dh')
OUT = os.path.join(ROOT, 'app', 'src', 'main', 'cpp', 'danha_main.o')


def _local_property(name):
    path = os.path.join(ROOT, 'local.properties')
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith(name + '='):
                return line.split('=', 1)[1].replace('\\\\:', ':').replace('\\\\\\\\', '\\\\')
    return None


def _find_android_sdk():
    candidates = [
        os.environ.get('ANDROID_HOME'),
        os.environ.get('ANDROID_SDK_ROOT'),
        _local_property('sdk.dir'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk'),
        os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Android', 'Sdk'),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None


def _version_key(path):
    parts = re.findall(r'\\d+', os.path.basename(path))
    return tuple(int(p) for p in parts) if parts else (0,)


def _find_ndk():
    candidates = [
        os.environ.get('ANDROID_NDK_HOME'),
        os.environ.get('ANDROID_NDK_ROOT'),
        _local_property('ndk.dir'),
    ]
    sdk = _find_android_sdk()
    if sdk:
        ndk_root = os.path.join(sdk, 'ndk')
        if os.path.isdir(ndk_root):
            candidates.extend(sorted(glob.glob(os.path.join(ndk_root, '*')), key=_version_key, reverse=True))
        bundle = os.path.join(sdk, 'ndk-bundle')
        candidates.append(bundle)
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None

danha_home = os.environ.get('DANHA_HOME')
if danha_home:
    sys.path.insert(0, danha_home)

try:
    from danha_compile import emit_android_ir, emit_android_object
except Exception as exc:
    raise SystemExit(
        'danha_compile.py를 찾을 수 없어. DANHA_HOME을 Danha 폴더로 지정해줘: ' + str(exc)
    )

with open(SRC, 'r', encoding='utf-8') as f:
    source = f.read()

try:
    emit_android_object(source, OUT, base_dir=os.path.dirname(SRC))
except Exception as exc:
    ll_path = os.path.join(ROOT, 'app', 'src', 'main', 'cpp', 'danha_main.ll')
    emit_android_ir(source, ll_path, base_dir=os.path.dirname(SRC))

    ndk = _find_ndk()
    if not ndk:
        raise SystemExit(
            'llvmlite Android object emission 실패: ' + str(exc) + '\\n'
            'danha_main.ll은 생성했지만 object 컴파일에는 Android NDK가 필요해. '
            'ANDROID_NDK_HOME, ANDROID_NDK_ROOT, local.properties의 ndk.dir, 또는 SDK ndk 폴더를 확인해줘.'
        )

    clang_pattern = 'aarch64-linux-android21-clang.cmd' if os.name == 'nt' else 'aarch64-linux-android21-clang'
    clang_candidates = glob.glob(os.path.join(
        ndk, 'toolchains', 'llvm', 'prebuilt', '*', 'bin', clang_pattern
    ))
    if not clang_candidates:
        raise SystemExit('Android NDK clang을 찾을 수 없어: ' + ndk)
    clang = clang_candidates[0]
    subprocess.check_call([clang, '-c', ll_path, '-o', OUT])

print('Android Danha object:', OUT)
''')

    _write_file(os.path.join(tools_dir, 'check_android_env.py'), '''\
#!/usr/bin/env python3
import glob
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_property(name):
    path = os.path.join(ROOT, 'local.properties')
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith(name + '='):
                return line.split('=', 1)[1].replace('\\\\:', ':').replace('\\\\\\\\', '\\\\')
    return None


def find_sdk():
    candidates = [
        os.environ.get('ANDROID_HOME'),
        os.environ.get('ANDROID_SDK_ROOT'),
        local_property('sdk.dir'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk'),
        os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Android', 'Sdk'),
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    return None


def find_ndk(sdk):
    candidates = [
        os.environ.get('ANDROID_NDK_HOME'),
        os.environ.get('ANDROID_NDK_ROOT'),
        local_property('ndk.dir'),
    ]
    if sdk:
        candidates.extend(sorted(glob.glob(os.path.join(sdk, 'ndk', '*')), reverse=True))
        candidates.append(os.path.join(sdk, 'ndk-bundle'))
    for path in candidates:
        if path and os.path.isdir(path):
            clang = glob.glob(os.path.join(path, 'toolchains', 'llvm', 'prebuilt', '*', 'bin', 'aarch64-linux-android21-clang*'))
            if clang:
                return path
    return None


sdk = find_sdk()
ndk = find_ndk(sdk)
checks = {
    'java': shutil.which('java') or (os.path.join(os.environ.get('JAVA_HOME', ''), 'bin', 'java.exe') if os.environ.get('JAVA_HOME') else None),
    'gradle': os.path.exists(os.path.join(ROOT, 'gradlew.bat')) or shutil.which('gradle'),
    'android_sdk': sdk,
    'android_ndk': ndk,
    'cmake': shutil.which('cmake'),
}

ok = True
for name, value in checks.items():
    present = bool(value and (value is True or os.path.exists(value) or shutil.which(str(value))))
    ok = ok and present
    print(('OK   ' if present else 'MISS ') + name + (': ' + str(value) if value else ''))

raise SystemExit(0 if ok else 1)
''')

    _write_file(os.path.join(tools_dir, 'build_android_apk.ps1'), '''\
$ErrorActionPreference = "Stop"

if (-not $env:DANHA_HOME) {
    throw "DANHA_HOME을 Danha 폴더로 지정해줘."
}

python "$PSScriptRoot\\build_android_object.py"
python "$PSScriptRoot\\check_android_env.py"

Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if (Test-Path ".\\gradlew.bat") {
        .\\gradlew.bat assembleDebug
    } else {
        gradle assembleDebug
    }
} finally {
    Pop-Location
}
''')

    _write_file(os.path.join(tools_dir, 'setup_android_toolchain.ps1'), '''\
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root = Split-Path $PSScriptRoot -Parent
$ToolRoot = Join-Path $Root ".android-toolchain"
$Downloads = Join-Path $ToolRoot "downloads"
$Sdk = Join-Path $ToolRoot "android-sdk"
New-Item -ItemType Directory -Force -Path $Downloads | Out-Null

function Get-File($Url, $Out) {
    if (Test-Path $Out) { return }
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Out
}

$JdkInfo = Invoke-RestMethod -Uri "https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=windows&heap_size=normal&vendor=eclipse"
$JdkPackage = $JdkInfo[0].binary.package
$JdkZip = Join-Path $Downloads $JdkPackage.name
$CmdZip = Join-Path $Downloads "commandlinetools-win-14742923_latest.zip"
$GradleZip = Join-Path $Downloads "gradle-8.0.2-bin.zip"

Get-File $JdkPackage.link $JdkZip
Get-File "https://dl.google.com/android/repository/commandlinetools-win-14742923_latest.zip" $CmdZip
Get-File "https://services.gradle.org/distributions/gradle-8.0.2-bin.zip" $GradleZip

$JdkDir = Join-Path $ToolRoot "jdk17"
if (-not (Test-Path $JdkDir)) {
    New-Item -ItemType Directory -Force -Path $JdkDir | Out-Null
    Expand-Archive -Force -Path $JdkZip -DestinationPath $JdkDir
}

$CmdDir = Join-Path $Sdk "cmdline-tools\\latest"
if (-not (Test-Path $CmdDir)) {
    $Tmp = Join-Path $ToolRoot "cmdline-tools-tmp"
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
    Expand-Archive -Force -Path $CmdZip -DestinationPath $Tmp
    New-Item -ItemType Directory -Force -Path (Split-Path $CmdDir -Parent) | Out-Null
    Move-Item -Force -Path (Join-Path $Tmp "cmdline-tools") -Destination $CmdDir
    Remove-Item -Recurse -Force $Tmp
}

$GradleDir = Join-Path $ToolRoot "gradle"
if (-not (Test-Path $GradleDir)) {
    New-Item -ItemType Directory -Force -Path $GradleDir | Out-Null
    Expand-Archive -Force -Path $GradleZip -DestinationPath $GradleDir
}

$env:JAVA_HOME = Get-ChildItem -Directory $JdkDir | Select-Object -First 1 -ExpandProperty FullName
$env:ANDROID_HOME = $Sdk
$env:ANDROID_SDK_ROOT = $Sdk
$env:GRADLE_HOME = Get-ChildItem -Directory $GradleDir | Select-Object -First 1 -ExpandProperty FullName
$env:Path = "$env:JAVA_HOME\\bin;$env:ANDROID_HOME\\cmdline-tools\\latest\\bin;$env:GRADLE_HOME\\bin;$env:Path"

@("y", "y", "y", "y", "y", "y", "y", "y", "y", "y", "y", "y") | & "$env:ANDROID_HOME\\cmdline-tools\\latest\\bin\\sdkmanager.bat" --sdk_root=$env:ANDROID_HOME --licenses
& "$env:ANDROID_HOME\\cmdline-tools\\latest\\bin\\sdkmanager.bat" --sdk_root=$env:ANDROID_HOME "platforms;android-34" "build-tools;34.0.0" "platform-tools" "ndk;26.3.11579264" "cmake;3.22.1"

$LocalProps = Join-Path $Root "local.properties"
@(
    "sdk.dir=$($env:ANDROID_HOME -replace '\\\\','/')",
    "ndk.dir=$((Join-Path $env:ANDROID_HOME 'ndk\\26.3.11579264') -replace '\\\\','/')"
) | Set-Content -Encoding UTF8 $LocalProps

Write-Host "Android toolchain ready:"
Write-Host "  JAVA_HOME=$env:JAVA_HOME"
Write-Host "  ANDROID_HOME=$env:ANDROID_HOME"
Write-Host "  GRADLE_HOME=$env:GRADLE_HOME"
''')

    # MainActivity.java
    _write_file(os.path.join(java_dir, 'MainActivity.java'), f'''\
package com.danha.{base_name};

import android.app.Activity;
import android.os.Bundle;

public class MainActivity extends Activity {{
    static {{ System.loadLibrary("{base_name}"); }}

    native void runDanha();

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        runDanha();
    }}
}}
''')

    # AndroidManifest.xml
    _write_file(os.path.join(app_dir, 'AndroidManifest.xml'), f'''\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.danha.{base_name}">
    <application
        android:label="{base_name}"
        android:theme="@android:style/Theme.NoTitleBar.Fullscreen">
        <activity android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
''')

    # README
    _write_file(os.path.join(output_dir, 'README.md'), f'''\
# {base_name} — Android 빌드

## 빌드 방법

1. 단아 소스는 프로젝트 안에 복사돼 있어:
   ```
   app/src/main/danha/main.dh
   ```

2. 단아 소스를 Android용 ARM64 오브젝트로 컴파일:
   ```
   set DANHA_HOME=C:\\path\\to\\Danha
   python tools\\build_android_object.py
   ```

3. Android Studio에서 {base_name}_android/ 를 열거나:
   ```
   cd {base_name}_android
   ./gradlew assembleDebug
   ```

   `DANHA_HOME`이 설정돼 있으면 Gradle `preBuild`가 자동으로 Danha object를 갱신해.

PowerShell helper:
   ```
   .\tools\build_android_apk.ps1
   ```

Portable toolchain setup:
   ```
   .\tools\setup_android_toolchain.ps1
   ```
   Android SDK 라이선스 동의 후 `local.properties`가 자동 생성돼.

## 요구 사항
- Android SDK (API 21+)
- NDK r25 이상
- CMake 3.22 이상
''')

    return output_dir


def main(args):
    if len(args) < 2:
        print("사용법: danha compile --target <ios|android> <파일.dh>")
        return 1

    target, source_path = args[0], args[1]

    if not os.path.exists(source_path):
        print(f"❌ 파일을 찾을 수 없어: {source_path}")
        return 1

    try:
        if target == 'ios':
            out = build_ios(source_path)
            print(f"✅ iOS 프로젝트 생성: {out}")
        elif target == 'android':
            out = build_android(source_path)
            print(f"✅ Android 프로젝트 생성: {out}")
        else:
            print(f"❌ 모르는 모바일 타겟: {target} (ios 또는 android)")
            return 1
        return 0
    except Exception as e:
        print(f"❌ 모바일 빌드 실패: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
