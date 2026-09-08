# 9Router Node Runner (GitHub Actions Component)

এই ডিরেক্টরিটি শুধুমাত্র GitHub Actions রানারের জন্য প্রস্তুত করা হয়েছে।

## 📁 ফাইল স্ট্রাকচার:
* `.github/workflows/node_runner.yml` : প্রধান GitHub Actions ওয়ার্কফ্লো।
* `scripts/seed_db.py` : হেডলেস সার্ভারে 9Router-এর OpenCode Free মডেল ও API Key স্বয়ংক্রিয়ভাবে সিড করার স্ক্রিপ্ট।

---

## 🚀 গিটহাবে পুশ ও রান করার নিয়ম:

### ১. গিটহাব রিপোজিটরিতে সিক্রেট যোগ করুন (Optional but Recommended):
আপনার GitHub Repo-এর **Settings > Secrets and variables > Actions > New repository secret**-এ গিয়ে:
* **Name:** `CF_TUNNEL_TOKEN`
* **Value:** আপনার ক্লাউডফ্লেয়ার টানেল টোকেনটি পেস্ট করুন।

---

### ২. ওয়ার্কফ্লো রান করার নিয়ম:
1. আপনার GitHub রিপোজিটরির **Actions** ট্যাবে যান।
2. বাঁপাশের তালিকা থেকে **"9Router Dedicated Node Runner"** ওয়ার্কফ্লো সিলেক্ট করুন।
3. **Run workflow** বাটনে ক্লিক করুন:
   * **Target Nodes** বক্সে যে নোডগুলো চালাতে চান তা লিখুন:
     * একক নোড: `9rt1` বা `9rt3`
     * একাধিক নোড (কমা দিয়ে): `9rt1, 9rt2, 9rt5`
4. **Run workflow**-এ ক্লিক করে লাইভ লগে প্রবেশ করুন।

---

### 🔢 ডাইনামিক পোর্ট ম্যাপিং:
* `9rt1` ──► Port `6001`
* `9rt2` ──► Port `6002`
* `9rt3` ──► Port `6003`
* `9rt320` ──► Port `6320`
