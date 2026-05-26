import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'dart:convert';
import 'package:image_picker/image_picker.dart';

void main() => runApp(const MyApp());

class MyApp extends StatelessWidget {
  const MyApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'ROULIN POST - Social Feed',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        primarySwatch: Colors.blueGrey,
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.blueGrey),
      ),
      home: const FeedScreen(),
    );
  }
}

class FeedScreen extends StatefulWidget {
  const FeedScreen({super.key});
  @override
  State<FeedScreen> createState() => _FeedScreenState();
}

class _FeedScreenState extends State<FeedScreen> {
  final String baseUrl = "https://roulinpost-3.onrender.com";
  List posts = [];
  Map<String, dynamic>? currentUser;
  bool isLoading = false;
  // --- NEW STATUS VARIABLE: Handles the floating upload indicator tracking state ---
  bool isPosting = false;
  String? viewingProfileOf;
  int offset = 0;
  final ScrollController _scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    fetchPosts();
    _scrollController.addListener(() {
      if (_scrollController.position.pixels >=
          _scrollController.position.maxScrollExtent - 200) {
        fetchPosts(append: true);
      }
    });
  }

  Future<void> fetchPosts({bool append = false}) async {
    if (isLoading) return;
    setState(() => isLoading = true);

    if (!append) {
      offset = 0;
      posts.clear();
    }

    String url = "$baseUrl/posts?limit=10&offset=$offset";
    if (viewingProfileOf != null) url += "&username=$viewingProfileOf";

    try {
      final response = await http.get(Uri.parse(url));
      if (response.statusCode == 200) {
        List newPosts = json.decode(response.body);
        setState(() {
          posts.addAll(newPosts);
          offset += newPosts.length;
        });
      }
    } catch (e) {
      debugPrint("Error fetching posts: $e");
    } finally {
      setState(() => isLoading = false);
    }
  }

  Future<void> handleLike(String postId, int index) async {
    final response = await http.post(Uri.parse("$baseUrl/posts/$postId/like"));
    if (response.statusCode == 200) {
      setState(() {
        posts[index]['likes'] += 1;
        posts[index]['isLikedByUser'] = true;
      });
    }
  }

  // --- UPGRADED FUNCTION: Includes Posting Status Ring & Forces Clear Refresh ---
  Future<void> createPost(String message, List<XFile> images) async {
    if (images.length > 5) return;

    // Activate the ring status loader notifications layout overlay
    setState(() => isPosting = true);

    try {
      var request = http.MultipartRequest('POST', Uri.parse("$baseUrl/posts"));
      request.fields['username'] = currentUser!['username'];
      request.fields['message'] = message;

      for (var img in images) {
        final bytes = await img.readAsBytes();
        final multipartFile = http.MultipartFile.fromBytes(
          'files',
          bytes,
          filename: img.name,
        );
        request.files.add(multipartFile);
      }

      var streamedResponse = await request.send();
      var response = await http.Response.fromStream(streamedResponse);

      if (response.statusCode == 200) {
        // Force the app to clear out current array metrics and pull the feed fresh from top
        await fetchPosts(append: false);
      }
    } catch (e) {
      debugPrint("Pipeline Error: $e");
    } finally {
      // Clean up the uploading notification states gracefully
      setState(() => isPosting = false);
    }
  }

  Future<void> updatePost(String postId, String message,
      List<dynamic> retainedImages, List<XFile> newImages) async {
    try {
      var request =
          http.MultipartRequest('PUT', Uri.parse("$baseUrl/posts/$postId"));
      request.fields['username'] = currentUser!['username'];
      request.fields['message'] = message;
      request.fields['retained_image_urls'] = json.encode(retainedImages);

      for (var img in newImages) {
        final bytes = await img.readAsBytes();
        final multipartFile = http.MultipartFile.fromBytes(
          'files',
          bytes,
          filename: img.name,
        );
        request.files.add(multipartFile);
      }

      var streamedResponse = await request.send();
      var response = await http.Response.fromStream(streamedResponse);
      if (response.statusCode == 200) {
        fetchPosts();
      }
    } catch (e) {
      debugPrint("Error editing post: $e");
    }
  }

  Future<void> deletePost(String postId) async {
    try {
      final res = await http.delete(
        Uri.parse(
            "$baseUrl/posts/$postId?username=${currentUser!['username']}"),
      );
      if (res.statusCode == 200) {
        fetchPosts();
      }
    } catch (e) {
      debugPrint("Error deleting post: $e");
    }
  }

  void openInstagramZoomView(List<dynamic> imageUrls, int initialIndex) {
    showDialog(
      context: context,
      builder: (context) => Dialog(
        backgroundColor: Colors.black.withOpacity(0.85),
        insetPadding: EdgeInsets.zero,
        child: Stack(
          alignment: Alignment.center,
          children: [
            SizedBox(
              width: MediaQuery.of(context).size.width,
              height: MediaQuery.of(context).size.height,
              child: PageView.builder(
                controller: PageController(initialPage: initialIndex),
                itemCount: imageUrls.length,
                physics: const BouncingScrollPhysics(),
                itemBuilder: (context, index) {
                  return Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 20, vertical: 40),
                    alignment: Alignment.center,
                    child: InteractiveViewer(
                      panEnabled: true,
                      minScale: 1.0,
                      maxScale: 4.0,
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(8),
                        child: Image.network(
                          imageUrls[index],
                          fit: BoxFit.contain,
                        ),
                      ),
                    ),
                  );
                },
              ),
            ),
            Positioned(
              top: 30,
              right: 30,
              child: IconButton(
                icon: const Icon(Icons.close, color: Colors.white, size: 32),
                onPressed: () => Navigator.pop(context),
              ),
            ),
            if (imageUrls.length > 1)
              Positioned(
                bottom: 30,
                child: Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                  decoration: BoxDecoration(
                    color: Colors.black54,
                    borderRadius: BorderRadius.circular(20),
                  ),
                  child: const Text(
                    "← Swipe Left or Right to Scroll →",
                    style: TextStyle(
                        color: Colors.white70,
                        fontSize: 13,
                        fontWeight: FontWeight.w500),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  void showAuthDialog() {
    final userCtrl = TextEditingController();
    final emailCtrl = TextEditingController();
    final passCtrl = TextEditingController();
    final otpCtrl = TextEditingController();

    bool isLoginMode = true;
    bool isVerifyingOTPMode = false;
    String errorMessage = "";
    bool statusSuccess = false;

    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          String titleText = "Log In";
          if (isVerifyingOTPMode) {
            titleText = "Email Code Authentication";
          } else if (!isLoginMode) {
            titleText = "Register New Account";
          }

          return AlertDialog(
            title: Text(titleText),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (errorMessage.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Text(
                      errorMessage,
                      style: TextStyle(
                        color: statusSuccess ? Colors.green : Colors.red,
                        fontWeight: FontWeight.bold,
                        fontSize: 13,
                      ),
                    ),
                  ),
                if (isVerifyingOTPMode) ...[
                  const Text(
                    "Enter the 6-digit verification code sent to your email to activate your account.",
                    style: TextStyle(fontSize: 13, color: Colors.black54),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: otpCtrl,
                    decoration: const InputDecoration(
                      labelText: "6-Digit OTP Code",
                      hintText: "123456",
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.number,
                  ),
                ] else ...[
                  TextField(
                    controller: userCtrl,
                    decoration: const InputDecoration(labelText: "Username"),
                  ),
                  if (!isLoginMode)
                    TextField(
                      controller: emailCtrl,
                      decoration: const InputDecoration(labelText: "Email"),
                      keyboardType: TextInputType.emailAddress,
                    ),
                  TextField(
                    controller: passCtrl,
                    obscureText: true,
                    decoration: const InputDecoration(labelText: "Password"),
                  ),
                  const SizedBox(height: 15),
                  TextButton(
                    onPressed: () {
                      setDialogState(() {
                        isLoginMode = !isLoginMode;
                        errorMessage = "";
                      });
                    },
                    child: Text(
                      isLoginMode
                          ? "Need an account? Register"
                          : "Already have an account? Log In",
                    ),
                  ),
                ],
              ],
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text("Cancel"),
              ),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                  backgroundColor: const Color(0xFFFFB347),
                  foregroundColor: Colors.black,
                ),
                onPressed: () async {
                  if (isVerifyingOTPMode) {
                    if (otpCtrl.text.trim().isEmpty) {
                      setDialogState(() =>
                          errorMessage = "Please enter verification pin.");
                      return;
                    }
                    try {
                      final res = await http.post(
                        Uri.parse("$baseUrl/auth/verify-otp"),
                        headers: {"Content-Type": "application/json"},
                        body: json.encode({
                          "username": userCtrl.text.trim(),
                          "otp_code": otpCtrl.text.trim(),
                        }),
                      );
                      if (res.statusCode == 200) {
                        setDialogState(() {
                          isVerifyingOTPMode = false;
                          isLoginMode = true;
                          errorMessage =
                              "Email verified! Please enter your password to log in.";
                          statusSuccess = true;
                          otpCtrl.clear();
                        });
                      } else {
                        final errorData = json.decode(res.body);
                        setDialogState(() {
                          errorMessage =
                              errorData['detail'] ?? "OTP validation mismatch";
                          statusSuccess = false;
                        });
                      }
                    } catch (e) {
                      setDialogState(() =>
                          errorMessage = "Connection error validating token.");
                    }
                    return;
                  }

                  if (userCtrl.text.trim().isEmpty ||
                      passCtrl.text.trim().isEmpty ||
                      (!isLoginMode && emailCtrl.text.trim().isEmpty)) {
                    setDialogState(() {
                      errorMessage = "Please fill all required fields.";
                      statusSuccess = false;
                    });
                    return;
                  }

                  if (isLoginMode) {
                    try {
                      final res = await http.post(
                        Uri.parse("$baseUrl/auth/login"),
                        headers: {"Content-Type": "application/json"},
                        body: json.encode({
                          "username": userCtrl.text.trim(),
                          "password": passCtrl.text.trim(),
                        }),
                      );
                      if (res.statusCode == 200) {
                        setState(() => currentUser = json.decode(res.body));
                        Navigator.pop(context);
                        fetchPosts();
                      } else {
                        final errorData = json.decode(res.body);
                        setDialogState(() {
                          errorMessage =
                              errorData['detail'] ?? "Invalid credentials";
                          statusSuccess = false;
                        });
                      }
                    } catch (e) {
                      setDialogState(
                          () => errorMessage = "Connection error to server.");
                    }
                  } else {
                    try {
                      final res = await http.post(
                        Uri.parse("$baseUrl/auth/register"),
                        headers: {"Content-Type": "application/json"},
                        body: json.encode({
                          "username": userCtrl.text.trim(),
                          "email": emailCtrl.text.trim(),
                          "password": passCtrl.text.trim(),
                        }),
                      );
                      if (res.statusCode == 200) {
                        setDialogState(() {
                          isVerifyingOTPMode = true;
                          errorMessage =
                              "Authentication email dispatched! Enter token.";
                          statusSuccess = true;
                          emailCtrl.clear();
                        });
                      } else {
                        final errorData = json.decode(res.body);
                        setDialogState(() {
                          errorMessage =
                              errorData['detail'] ?? "Registration failed.";
                          statusSuccess = false;
                        });
                      }
                    } catch (e) {
                      setDialogState(
                          () => errorMessage = "Connection error to server.");
                    }
                  }
                },
                child: Text(isVerifyingOTPMode
                    ? "Verify"
                    : (isLoginMode ? "Login" : "Register")),
              ),
            ],
          );
        },
      ),
    );
  }

  void showUserDashboardDialog() {
    final nameCtrl = TextEditingController(text: currentUser!['username']);
    final mailCtrl = TextEditingController(text: currentUser!['email']);
    final newPassCtrl = TextEditingController();
    XFile? chosenAvatar;
    final ImagePicker picker = ImagePicker();
    String dashboardMessage = "";
    bool operationSuccess = false;

    showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDashboardState) => AlertDialog(
          title: Row(
            children: [
              const Icon(Icons.dashboard, color: Colors.blueGrey),
              const SizedBox(width: 8),
              Text("${currentUser!['username']}'s Settings Dashboard"),
            ],
          ),
          content: SizedBox(
            width: 400,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (dashboardMessage.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Text(
                        dashboardMessage,
                        style: TextStyle(
                          color: operationSuccess ? Colors.green : Colors.red,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    ),
                  Center(
                    child: Column(
                      children: [
                        CircleAvatar(
                          radius: 40,
                          backgroundColor: Colors.grey[300],
                          backgroundImage: chosenAvatar != null
                              ? NetworkImage(chosenAvatar!.path)
                              : (currentUser!['profile_url'] != null &&
                                      currentUser!['profile_url']
                                          .toString()
                                          .isNotEmpty
                                  ? NetworkImage(currentUser!['profile_url'])
                                  : null),
                          child: chosenAvatar == null &&
                                  (currentUser!['profile_url'] == null ||
                                      currentUser!['profile_url']
                                          .toString()
                                          .isEmpty)
                              ? const Icon(Icons.person,
                                  size: 40, color: Colors.grey)
                              : null,
                        ),
                        TextButton.icon(
                          icon: const Icon(Icons.camera_alt, size: 16),
                          label: const Text("Change Profile Photo"),
                          onPressed: () async {
                            final XFile? img = await picker.pickImage(
                                source: ImageSource.gallery);
                            if (img != null) {
                              setDashboardState(() => chosenAvatar = img);
                            }
                          },
                        ),
                      ],
                    ),
                  ),
                  const SizedBox(height: 15),
                  TextField(
                    controller: nameCtrl,
                    decoration: const InputDecoration(
                        labelText: "Edit Username",
                        border: OutlineInputBorder()),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: mailCtrl,
                    decoration: const InputDecoration(
                        labelText: "Edit Email Address",
                        border: OutlineInputBorder()),
                    keyboardType: TextInputType.emailAddress,
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: newPassCtrl,
                    obscureText: true,
                    decoration: const InputDecoration(
                      labelText: "New Password (Leave blank to keep current)",
                      border: OutlineInputBorder(),
                    ),
                  ),
                  const SizedBox(height: 20),
                  const Divider(color: Colors.redAccent),
                  const SizedBox(height: 5),
                  Center(
                    child: TextButton.icon(
                      style: TextButton.styleFrom(foregroundColor: Colors.red),
                      icon: const Icon(Icons.delete_forever),
                      label: const Text("COMPLETELY DELETE ACCOUNT",
                          style: TextStyle(fontWeight: FontWeight.bold)),
                      onPressed: () async {
                        bool confirmDelete = await showDialog(
                              context: context,
                              builder: (ctx) => AlertDialog(
                                title:
                                    const Text("Confirm Account Destruction"),
                                content: const Text(
                                    "Are you absolutely sure? This action will permanently erase your credentials out of Firestore database. This action cannot be undone."),
                                actions: [
                                  TextButton(
                                      onPressed: () =>
                                          Navigator.pop(ctx, false),
                                      child: const Text("Cancel")),
                                  ElevatedButton(
                                    style: ElevatedButton.styleFrom(
                                        backgroundColor: Colors.red,
                                        foregroundColor: Colors.white),
                                    onPressed: () => Navigator.pop(ctx, true),
                                    child: const Text("DELETE PERMANENTLY"),
                                  ),
                                ],
                              ),
                            ) ??
                            false;

                        if (confirmDelete) {
                          try {
                            final res = await http.delete(
                              Uri.parse(
                                  "$baseUrl/auth/profile/${currentUser!['username']}"),
                            );
                            if (res.statusCode == 200) {
                              Navigator.pop(context);
                              setState(() {
                                currentUser = null;
                              });
                              ScaffoldMessenger.of(context).showSnackBar(
                                const SnackBar(
                                    content:
                                        Text("Account successfully deleted.")),
                              );
                              fetchPosts();
                            }
                          } catch (e) {
                            setDashboardState(() => dashboardMessage =
                                "Error connecting to service.");
                          }
                        }
                      },
                    ),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text("Close")),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                  backgroundColor: Colors.blueGrey,
                  foregroundColor: Colors.white),
              onPressed: () async {
                if (nameCtrl.text.trim().isEmpty ||
                    mailCtrl.text.trim().isEmpty) {
                  setDashboardState(() => dashboardMessage =
                      "Username and Email fields cannot be blank.");
                  return;
                }
                try {
                  var request = http.MultipartRequest(
                      'PUT',
                      Uri.parse(
                          "$baseUrl/auth/profile/${currentUser!['username']}"));
                  request.fields['new_username'] = nameCtrl.text.trim();
                  request.fields['new_email'] = mailCtrl.text.trim();
                  if (newPassCtrl.text.trim().isNotEmpty) {
                    request.fields['new_password'] = newPassCtrl.text.trim();
                  }

                  if (chosenAvatar != null) {
                    final bytes = await chosenAvatar!.readAsBytes();
                    request.files.add(http.MultipartFile.fromBytes(
                      'avatar_file',
                      bytes,
                      filename: chosenAvatar!.name,
                    ));
                  }

                  var streamedRes = await request.send();
                  var response = await http.Response.fromStream(streamedRes);

                  if (response.statusCode == 200) {
                    setState(() {
                      currentUser = json.decode(response.body);
                    });
                    setDashboardState(() {
                      dashboardMessage = "Profile metrics saved cleanly!";
                      operationSuccess = true;
                    });
                  } else {
                    final err = json.decode(response.body);
                    setDashboardState(() {
                      dashboardMessage =
                          err['detail'] ?? "Update error encountered.";
                      operationSuccess = false;
                    });
                  }
                } catch (e) {
                  setDashboardState(
                      () => dashboardMessage = "Connection processing error.");
                }
              },
              child: const Text("SAVE CHANGES"),
            ),
          ],
        ),
      ),
    );
  }

  void showCreatePostDialog() async {
    final msgCtrl = TextEditingController();
    List<XFile> selectedImages = [];
    final ImagePicker picker = ImagePicker();
    String errorString = "";

    showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text("Create New Post"),
          content: SizedBox(
            width: 450,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (errorString.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: Text(errorString,
                        style: const TextStyle(
                            color: Colors.red, fontWeight: FontWeight.bold)),
                  ),
                TextField(
                  controller: msgCtrl,
                  maxLines: 4,
                  decoration: const InputDecoration(
                    hintText: "What's on your mind?",
                    border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 15),
                OutlinedButton.icon(
                  style: OutlinedButton.styleFrom(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 12),
                  ),
                  icon: const Icon(Icons.add_a_photo, color: Colors.blueGrey),
                  label: const Text("Choose Photos",
                      style: TextStyle(color: Colors.black87)),
                  onPressed: () async {
                    final List<XFile>? images = await picker.pickMultiImage();
                    if (images != null) {
                      if (images.length > 5) {
                        setDialogState(() {
                          errorString =
                              "⚠️ Selection Denied: You can select a maximum of 5 images only.";
                        });
                      } else {
                        setDialogState(() {
                          selectedImages = images;
                          errorString = "";
                        });
                      }
                    }
                  },
                ),
                if (selectedImages.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 10, left: 4),
                    child: Text(
                      "🎉 ${selectedImages.length} images selected ready to post",
                      style: const TextStyle(
                          color: Colors.green, fontWeight: FontWeight.w500),
                    ),
                  ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text("Cancel"),
            ),
            ElevatedButton(
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.blueGrey,
                foregroundColor: Colors.white,
              ),
              onPressed: () {
                if (selectedImages.length > 5) return;
                createPost(msgCtrl.text, selectedImages);
                Navigator.pop(context);
              },
              child: const Text("POST"),
            ),
          ],
        ),
      ),
    );
  }

  void showEditPostDialog(Map<String, dynamic> post) {
    final msgCtrl = TextEditingController(text: post['message']);
    List<dynamic> retainedUrls = List.from(post['image_urls']);
    List<XFile> freshlyChosenFiles = [];
    final ImagePicker picker = ImagePicker();
    String dialogError = "";

    showDialog(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setEditDialogState) {
          int currentTotalCount =
              retainedUrls.length + freshlyChosenFiles.length;

          return AlertDialog(
            title: const Text("Edit Your Post"),
            content: SizedBox(
              width: 500,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (dialogError.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: Text(dialogError,
                            style: const TextStyle(
                                color: Colors.red,
                                fontWeight: FontWeight.bold)),
                      ),
                    TextField(
                      controller: msgCtrl,
                      maxLines: 3,
                      decoration: const InputDecoration(
                          labelText: "Edit Message Text",
                          border: OutlineInputBorder()),
                    ),
                    const SizedBox(height: 15),
                    const Text("Current Images (Max 5 total):",
                        style: TextStyle(
                            fontWeight: FontWeight.bold, fontSize: 13)),
                    const SizedBox(height: 8),
                    if (retainedUrls.isEmpty && freshlyChosenFiles.isEmpty)
                      const Text("No images attached to this post.",
                          style: TextStyle(color: Colors.grey, fontSize: 13)),
                    if (retainedUrls.isNotEmpty)
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: retainedUrls.map((url) {
                          return Stack(
                            children: [
                              ClipRRect(
                                borderRadius: BorderRadius.circular(6),
                                child: Image.network(url,
                                    width: 80, height: 80, fit: BoxFit.cover),
                              ),
                              Positioned(
                                top: 0,
                                right: 0,
                                child: GestureDetector(
                                  onTap: () => setEditDialogState(
                                      () => retainedUrls.remove(url)),
                                  child: Container(
                                    color: Colors.black54,
                                    child: const Icon(Icons.close,
                                        color: Colors.white, size: 18),
                                  ),
                                ),
                              )
                            ],
                          );
                        }).toList(),
                      ),
                    if (freshlyChosenFiles.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      const Text("Newly Appended Photos:",
                          style: TextStyle(
                              color: Colors.blueGrey,
                              fontSize: 12,
                              fontWeight: FontWeight.w500)),
                      const SizedBox(height: 4),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: freshlyChosenFiles.map((file) {
                          return Stack(
                            children: [
                              Container(
                                width: 80,
                                height: 80,
                                decoration: BoxDecoration(
                                  border: Border.all(color: Colors.green),
                                  borderRadius: BorderRadius.circular(6),
                                ),
                                child: const Icon(Icons.image,
                                    color: Colors.green),
                              ),
                              Positioned(
                                top: 0,
                                right: 0,
                                child: GestureDetector(
                                  onTap: () => setEditDialogState(
                                      () => freshlyChosenFiles.remove(file)),
                                  child: Container(
                                    color: Colors.black54,
                                    child: const Icon(Icons.close,
                                        color: Colors.white, size: 18),
                                  ),
                                ),
                              )
                            ],
                          );
                        }).toList(),
                      ),
                    ],
                    const SizedBox(height: 15),
                    OutlinedButton.icon(
                      icon: const Icon(Icons.add_photo_alternate),
                      label: const Text("Append More Photos"),
                      onPressed: currentTotalCount >= 5
                          ? null
                          : () async {
                              final List<XFile>? chosen =
                                  await picker.pickMultiImage();
                              if (chosen != null) {
                                if (retainedUrls.length +
                                        freshlyChosenFiles.length +
                                        chosen.length >
                                    5) {
                                  setEditDialogState(() => dialogError =
                                      "⚠️ Total post images cannot exceed 5.");
                                } else {
                                  setEditDialogState(() {
                                    freshlyChosenFiles.addAll(chosen);
                                    dialogError = "";
                                  });
                                }
                              }
                            },
                    ),
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: const Text("Cancel")),
              ElevatedButton(
                style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blueGrey,
                    foregroundColor: Colors.white),
                onPressed: () async {
                  if (retainedUrls.length + freshlyChosenFiles.length > 5)
                    return;
                  await updatePost(post['id'], msgCtrl.text, retainedUrls,
                      freshlyChosenFiles);
                  Navigator.pop(context);
                },
                child: const Text("SAVE UPDATES"),
              )
            ],
          );
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    bool isUserLoggedIn = currentUser != null;

    return Scaffold(
      appBar: AppBar(
        leading: viewingProfileOf != null
            ? IconButton(
                icon: const Icon(Icons.arrow_back),
                onPressed: () => setState(() {
                  viewingProfileOf = null;
                  fetchPosts();
                }),
              )
            : null,
        title: Text(
          viewingProfileOf != null
              ? "@$viewingProfileOf's Feed"
              : "ROULIN POST - Social Feed",
          style:
              const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
        ),
        backgroundColor: Colors.blueGrey,
        iconTheme: const IconThemeData(color: Colors.white),
        actions: [
          if (isUserLoggedIn)
            IconButton(
              icon: const Icon(Icons.settings, color: Colors.white),
              tooltip: "Dashboard Settings",
              onPressed: showUserDashboardDialog,
            ),
          if (viewingProfileOf != null)
            IconButton(
              icon: const Icon(Icons.home),
              onPressed: () => setState(() {
                viewingProfileOf = null;
                fetchPosts();
              }),
            ),
          Padding(
            padding: const EdgeInsets.only(right: 12),
            key: const ValueKey('auth_btn_wrapper'),
            child: TextButton(
              onPressed: currentUser == null
                  ? showAuthDialog
                  : () => setState(() => currentUser = null),
              child: Text(
                currentUser == null ? "LOG IN" : "LOG OUT",
                style: const TextStyle(
                    color: Colors.white, fontWeight: FontWeight.bold),
              ),
            ),
          ),
        ],
      ),
      // --- UPGRADED BODY: Stack layout maps ring notifications over the scroll list ---
      body: Stack(
        children: [
          ListView.builder(
            controller: _scrollController,
            itemCount: posts.length + (isLoading ? 1 : 0),
            itemBuilder: (context, index) {
              if (index == posts.length) {
                return const Center(child: CircularProgressIndicator());
              }
              final post = posts[index];
              bool isLiked = post['isLikedByUser'] == true;

              bool isPostOwner = isUserLoggedIn &&
                  (currentUser!['username'].toString().toLowerCase() ==
                      post['username'].toString().toLowerCase());

              return Card(
                margin:
                    const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                elevation: 2,
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          GestureDetector(
                            onTap: () => setState(() {
                              viewingProfileOf = post['username'];
                              fetchPosts();
                            }),
                            child: Row(
                              children: [
                                Container(
                                  width: 32,
                                  height: 32,
                                  decoration: const BoxDecoration(
                                      shape: BoxShape.circle),
                                  child: post['user_avatar'] != null &&
                                          post['user_avatar']
                                              .toString()
                                              .isNotEmpty
                                      ? ClipRRect(
                                          borderRadius:
                                              BorderRadius.circular(16),
                                          child: Image.network(
                                            post['user_avatar'],
                                            fit: BoxFit.cover,
                                            errorBuilder: (context, error,
                                                    stackTrace) =>
                                                const Icon(Icons.account_circle,
                                                    color: Colors.grey,
                                                    size: 32),
                                          ),
                                        )
                                      : const Icon(Icons.account_circle,
                                          color: Colors.grey, size: 32),
                                ),
                                const SizedBox(width: 8),
                                Text(
                                  "@${post['username']}",
                                  style: const TextStyle(
                                      fontWeight: FontWeight.bold,
                                      color: Colors.blue,
                                      fontSize: 15),
                                ),
                              ],
                            ),
                          ),
                          Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              if (isPostOwner) ...[
                                IconButton(
                                  icon: const Icon(Icons.edit,
                                      color: Colors.orange, size: 20),
                                  tooltip: "Edit Post",
                                  constraints: const BoxConstraints(),
                                  padding:
                                      const EdgeInsets.symmetric(horizontal: 8),
                                  onPressed: () => showEditPostDialog(post),
                                ),
                                IconButton(
                                  icon: const Icon(Icons.delete,
                                      color: Colors.redAccent, size: 20),
                                  tooltip: "Delete Post",
                                  constraints: const BoxConstraints(),
                                  padding:
                                      const EdgeInsets.symmetric(horizontal: 8),
                                  onPressed: () async {
                                    bool confirm = await showDialog(
                                          context: context,
                                          builder: (ctx) => AlertDialog(
                                            title: const Text("Delete Post"),
                                            content: const Text(
                                                "Remove this post permanently? Attached images will be deleted from Storage."),
                                            actions: [
                                              TextButton(
                                                  onPressed: () =>
                                                      Navigator.pop(ctx, false),
                                                  child: const Text("Cancel")),
                                              ElevatedButton(
                                                style: ElevatedButton.styleFrom(
                                                    backgroundColor:
                                                        Colors.red),
                                                onPressed: () =>
                                                    Navigator.pop(ctx, true),
                                                child: const Text(
                                                    "Confirm Delete"),
                                              )
                                            ],
                                          ),
                                        ) ??
                                        false;
                                    if (confirm) {
                                      await deletePost(post['id']);
                                    }
                                  },
                                ),
                                const SizedBox(width: 4),
                              ],
                              IconButton(
                                icon: Icon(
                                    isLiked
                                        ? Icons.favorite
                                        : Icons.favorite_border,
                                    color: Colors.red,
                                    size: 20),
                                constraints: const BoxConstraints(),
                                padding:
                                    const EdgeInsets.only(left: 8, right: 4),
                                onPressed: () => handleLike(post['id'], index),
                              ),
                              Padding(
                                padding: const EdgeInsets.only(right: 4),
                                child: Text("${post['likes']}",
                                    style: const TextStyle(
                                        color: Colors.blueGrey, fontSize: 14)),
                              ),
                            ],
                          ),
                        ],
                      ),
                      if (post['message'] != null &&
                          post['message'].toString().isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          child: Text(post['message'],
                              style: const TextStyle(fontSize: 15)),
                        ),
                      if (post['image_urls'] != null &&
                          post['image_urls'].isNotEmpty)
                        Container(
                          height: 250,
                          margin: const EdgeInsets.only(top: 8),
                          child: ListView.builder(
                            scrollDirection: Axis.horizontal,
                            itemCount: post['image_urls'].length,
                            itemBuilder: (ctx, imgIndex) => Padding(
                              padding: const EdgeInsets.only(right: 12),
                              child: GestureDetector(
                                onTap: () => openInstagramZoomView(
                                    post['image_urls'], imgIndex),
                                child: ClipRRect(
                                  borderRadius: BorderRadius.circular(10),
                                  child: Image.network(
                                    post['image_urls'][imgIndex],
                                    width: 250,
                                    height: 250,
                                    fit: BoxFit.cover,
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              );
            },
          ),

          // --- NEW CORE UI DESIGN: Ring Notification HUD mapping triggers when isPosting is running ---
          if (isPosting)
            Container(
              color: Colors.black.withOpacity(0.4),
              child: const Center(
                child: Card(
                  elevation: 4,
                  child: Padding(
                    padding: EdgeInsets.symmetric(horizontal: 28, vertical: 20),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        CircularProgressIndicator(strokeWidth: 3.5),
                        SizedBox(height: 16),
                        Text(
                          "Uploading Post Assets...",
                          style: TextStyle(
                              fontWeight: FontWeight.w600, fontSize: 14),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
      bottomNavigationBar: isUserLoggedIn
          ? BottomAppBar(
              child: Center(
                child: ElevatedButton.icon(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blueGrey,
                    foregroundColor: Colors.white,
                    minimumSize: const Size(200, 45),
                  ),
                  onPressed: showCreatePostDialog,
                  icon: const Icon(Icons.edit),
                  label: const Text("POST SOMETHING"),
                ),
              ),
            )
          : null,
    );
  }
}
