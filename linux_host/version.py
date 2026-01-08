import os

class Version:

    # Parses the common plugin version from the pyproject.toml file.
    # Throws if the file can't be found or the version string can't be found.
    # This logic is shared with the moonraker installer!
    @staticmethod
    def GetPluginVersion(repoRoot:str) -> str:
        # Since OctoPrint says the version must be in the pyproject.toml file, we share the same file to reduce any duplication.
        projectTomlFilePath = os.path.join(repoRoot, "pyproject.toml")
        if os.path.exists(projectTomlFilePath) is False:
            raise Exception("Failed to find our repo root pyproject.toml file to parse the version string. Expected Path: "+projectTomlFilePath)

        # Read the file, find the version string.
        expectedVersionKey = "version"
        versionLine = None
        with open(projectTomlFilePath, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines:
                if line.startswith(expectedVersionKey):
                    versionLine = line
                    break

        # Make sure we found it.
        if versionLine is None:
            raise Exception("Failed to find a line that starts with '"+expectedVersionKey+"' in pyproject.toml file: "+projectTomlFilePath)

        # Parse the line
        firstQuote = versionLine.find('"')
        if firstQuote == -1:
            raise Exception("Failed to first quote in version line '"+versionLine+"'")
        firstQuote += 1 # Move past it
        secondQuote = versionLine.find('"', firstQuote)
        if secondQuote == -1:
            raise Exception("Failed to second quote in version line '"+versionLine+"'")

        # Parse the version string
        return versionLine[firstQuote:secondQuote]
