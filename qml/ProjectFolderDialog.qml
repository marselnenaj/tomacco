import QtQuick
import QtQuick.Controls
import "."

// The project folder chooser of every entry point (one instance in NativeWorkspace,
// which Workspace opens through its projectChooser): the app's own chooser
// (FileChooserDialog) in folder mode, so it follows the interface size, theme, language
// and phone layout like every other file and folder entry point. Qt's stock FolderDialog
// fallback kept a fixed 600x400 window with 16 px icons and one-line labels at any
// interface size, so at 175 % long folder names and the path were cut off. It starts in
// the folder that holds the open project and marks it; Open chooses the folder the
// location names when it can be a project (Backend::projectFolder: its canonical path,
// no app bundle); Cancel and Escape leave the current project as it is.
FileChooserDialog {
    id: dialog
    // The open project: the chooser starts in the folder that holds it and marks it.
    property string currentProject: ""
    signal chosen(string path)
    mode: "folder"
    namePrefix: "projectFolder"
    markPath: currentProject
    placeholder: qsTranslate("Workspace","Type the folder of a Swift, Kotlin or Flutter project")
    folderTarget: path => Studio.projectFolder(path)
    startAt: () => {
        const project=currentProject || (Studio.recentProjects.length?Studio.recentProjects[0]:"")
        if(!project)return "~"
        const parentFolder=Studio.folderListing(project).parent
        return parentFolder || project
    }
    onAccepted: if(selectedPath)chosen(selectedPath)
}
