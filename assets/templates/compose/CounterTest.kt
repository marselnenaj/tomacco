package __BUNDLE__

import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class CounterTest {
    @get:Rule
    val compose = createComposeRule()

    @Test
    fun addOneCountsAndResetClears() {
        var saved = -1
        compose.setContent { Counter(title = "Test", initial = 0, onChange = { saved = it }) }
        compose.onNodeWithTag("count").assertTextEquals("0")
        compose.onNodeWithText("Add one").performClick()
        compose.onNodeWithTag("count").assertTextEquals("1")
        assertEquals(1, saved)
        compose.onNodeWithText("Reset").performClick()
        compose.onNodeWithTag("count").assertTextEquals("0")
        assertEquals(0, saved)
    }
}
