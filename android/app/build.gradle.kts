plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

fun git(vararg args: String): String =
    providers.exec { commandLine("git", *args); isIgnoreExitValue = true }.standardOutput.asText.get().trim()

android {
    namespace = "com.madboly.netusage"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.madboly.netusage"
        minSdk = 28
        targetSdk = 35
        // Numbered from the repository, so Settings > Status shows which build is on the phone.
        versionCode = git("rev-list", "--count", "HEAD").toIntOrNull() ?: 1
        versionName = "1.$versionCode (${git("rev-parse", "--short", "HEAD")})"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Personal app, installed straight from this laptop: the debug key is enough.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    // A family phone's copy is built from its own assets folder (tools/add_family_phone.sh),
    // which holds only that phone's settings: never my history or my key.
    (project.findProperty("netusage.assets") as String?)?.let { dir ->
        sourceSets.getByName("main").assets.setSrcDirs(listOf(dir))
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.4")
    implementation("androidx.activity:activity-compose:1.9.1")

    val composeBom = platform("androidx.compose:compose-bom:2024.06.00")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")

    implementation("androidx.work:work-runtime-ktx:2.9.1")

    testImplementation("junit:junit:4.13.2")
}
