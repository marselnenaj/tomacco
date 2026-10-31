package __BUNDLE__

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val preferences = getPreferences(MODE_PRIVATE)
        setContent {
            MaterialTheme {
                Counter(
                    title = "__NAME__",
                    initial = preferences.getInt("count", 0),
                    onChange = { preferences.edit().putInt("count", it).apply() },
                )
            }
        }
    }
}

/** A counter that starts at [initial] and reports every change to [onChange]. */
@Composable
fun Counter(title: String, initial: Int, onChange: (Int) -> Unit) {
    var count by rememberSaveable { mutableIntStateOf(initial) }
    Scaffold { padding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(padding).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(title, style = MaterialTheme.typography.headlineMedium)
            Text(
                "$count",
                style = MaterialTheme.typography.displayLarge,
                modifier = Modifier.testTag("count"),
            )
            Button(onClick = { count += 1; onChange(count) }) { Text("Add one") }
            OutlinedButton(onClick = { count = 0; onChange(0) }) { Text("Reset") }
        }
    }
}

@Preview(showBackground = true)
@Composable
fun CounterPreview() {
    MaterialTheme { Counter(title = "__NAME__", initial = 3, onChange = {}) }
}
