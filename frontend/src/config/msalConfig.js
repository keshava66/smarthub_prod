export const msalConfig = {
  auth: {
    clientId: "574b1b8c-3ee9-44ef-9950-b29cab62a260", // Application (client) ID
    authority: "https://login.microsoftonline.com/3ac94b33-9135-4821-9502-eafda6592a35", // Directory (tenant) ID
    redirectUri: window.location.origin, // Dynamically uses current origin
    postLogoutRedirectUri: window.location.origin + "/login", // Redirect to login after logout
    navigateToLoginRequestUrl: false,
  },
  cache: {
    cacheLocation: "localStorage", // Store tokens in localStorage for persistence
    storeAuthStateInCookie: false, // Set to true if you have issues on IE11 or Edge
  },
  system: {
    loggerOptions: {
      loggerCallback: (level, message, containsPii) => {
        if (containsPii) {
          return;
        }
        switch (level) {
          case 0: // Error
            console.error(message);

            return;
          default:
            return;
        }
      },
    },
  },
};

// Scopes required for the application
export const loginRequest = {
  scopes: [
    "openid",
    "profile",
    "email",
    "User.Read",
    "User.Read.All",
    "Group.Read.All",
    "GroupMember.Read.All",
    "offline_access"
  ],
};

// Optional: Additional graph scopes if you need more user data
export const graphConfig = {
  graphMeEndpoint: "https://graph.microsoft.com/v1.0/me",
  graphMePhotoEndpoint: "https://graph.microsoft.com/v1.0/me/photo/$value",
  graphMeMemberOfEndpoint: "https://graph.microsoft.com/v1.0/me/memberOf",
  graphUsersEndpoint: "https://graph.microsoft.com/v1.0/users",
  graphGroupsEndpoint: "https://graph.microsoft.com/v1.0/groups"
};