from __future__ import annotations

import base64
import click
import json
import m3u8 # type: ignore
import os
import re
import requests
import secrets
import time
import uuid

from datetime import datetime, timedelta 

from vinetrimmer.config import directories
from vinetrimmer.objects import MenuTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.collections import as_list
from vinetrimmer.utils.io import get_ip_info

# GraphQL query
# REQUEST_DEVICE_CODE = """mutation requestLicensePlate($input: RequestLicensePlateInput!) { requestLicensePlate(requestLicensePlate: $input) { licensePlate expirationTime expiresInSeconds } }"""
CHECK_EMAIL = """query check($email: String!) { check(email: $email) { operations nextOperation } }"""
LOGIN = """mutation login($input: LoginInput!, $includeIdentity: Boolean!, $includeAccountConsentToken: Boolean!) { login(login: $input) { account { __typename ...accountGraphFragment } actionGrant activeSession { __typename ...sessionGraphFragment } identity @include(if: $includeIdentity) { __typename ...identityGraphFragment } } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }  fragment accountGraphFragment on Account { id umpMessages { data { messages { messageId messageSource displayLocations content } } } accountConsentToken @include(if: $includeAccountConsentToken) activeProfile { id umpMessages { data { messages { messageId content } } } } profiles { __typename ...profileGraphFragment } profileRequirements { primaryProfiles { personalInfo { requiresCollection } } secondaryProfiles { personalInfo { requiresCollection } personalInfoJrMode { requiresCollection } } } parentalControls { isProfileCreationProtected } flows { star { isOnboarded } } attributes { email emailVerified userVerified maxNumberOfProfilesAllowed locations { manual { country } purchase { country } registration { geoIp { country } } } } }  fragment sessionGraphFragment on Session { sessionId device { id } entitlements experiments { featureId variantId version } features { coPlay download noAds } homeLocation { countryCode adsSupported } inSupportedLocation isSubscriber location { countryCode adsSupported } portabilityLocation { countryCode } preferredMaturityRating { impliedMaturityRating ratingSystem } }  fragment identityGraphFragment on Identity { id email repromptSubscriberAgreement attributes { passwordResetRequired } commerce { notifications { subscriptionId type showNotification offerData { productType expectedTransition { date price { amount currency } } cypherKeys { key value type } } currentOffer { offerId price { amount currency frequency } } } } flows { marketingPreferences { isOnboarded eligibleForOnboarding } personalInfo { eligibleForCollection requiresCollection } } personalInfo { dateOfBirth gender } locations { purchase { country } } subscriber { subscriberStatus subscriptionAtRisk overlappingSubscription doubleBilled doubleBilledProviders subscriptions { id groupId state partner isEntitled source { sourceProvider sourceType subType sourceRef } product { id sku name entitlements { id name partner } bundle subscriptionPeriod earlyAccess trial { duration } categoryCodes } stacking { status overlappingSubscriptionProviders previouslyStacked previouslyStackedByProvider } term { purchaseDate startDate expiryDate nextRenewalDate pausedDate churnedDate isFreeTrial } } } consent { id idType token } }"""
LOGIN_ACTION_GRANT = """mutation loginWithActionGrant($input: LoginWithActionGrantInput!, $includeAccountConsentToken: Boolean!) { loginWithActionGrant(login: $input) { account { __typename ...accountGraphFragment } activeSession { __typename ...sessionGraphFragment } identity { __typename ...identityGraphFragment } actionGrant } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo backgroundAudio prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }  fragment accountGraphFragment on Account { id umpMessages { data { messages { messageId messageSource displayLocations content } } } accountConsentToken @include(if: $includeAccountConsentToken) activeProfile { id umpMessages { data { messages { messageId content } } } } profiles { __typename ...profileGraphFragment } profileRequirements { primaryProfiles { personalInfo { requiresCollection } } secondaryProfiles { personalInfo { requiresCollection } personalInfoJrMode { requiresCollection } } } parentalControls { isProfileCreationProtected } flows { star { isOnboarded } } attributes { email emailVerified userVerified maxNumberOfProfilesAllowed locations { manual { country } purchase { country } registration { geoIp { country } } } } }  fragment sessionGraphFragment on Session { sessionId device { id } entitlements experiments { featureId variantId version } features { coPlay download noAds } homeLocation { countryCode adsSupported } inSupportedLocation isSubscriber location { countryCode adsSupported } portabilityLocation { countryCode } preferredMaturityRating { impliedMaturityRating ratingSystem } }  fragment identityGraphFragment on Identity { id email repromptSubscriberAgreement attributes { passwordResetRequired } commerce { notifications { subscriptionId type showNotification offerData { productType expectedTransition { date price { amount currency } } cypherKeys { key value type } } currentOffer { offerId price { amount currency frequency } } } } flows { marketingPreferences { isOnboarded eligibleForOnboarding } personalInfo { eligibleForCollection requiresCollection } } personalInfo { dateOfBirth gender } locations { purchase { country } } subscriber { subscriberStatus subscriptionAtRisk overlappingSubscription doubleBilled doubleBilledProviders subscriptions { id groupId state partner isEntitled source { sourceProvider sourceType subType sourceRef } product { id sku name entitlements { id name partner } bundle subscriptionPeriod earlyAccess trial { duration } categoryCodes } stacking { status overlappingSubscriptionProviders previouslyStacked previouslyStackedByProvider } term { purchaseDate startDate expiryDate nextRenewalDate pausedDate churnedDate isFreeTrial } } } consent { id idType token } }"""
LOGIN_OTP = """mutation authenticateWithOtp($input: AuthenticateWithOtpInput!) { authenticateWithOtp(authenticateWithOtp: $input) { actionGrant securityAction passwordRules { __typename ...passwordRulesFragment } } }  fragment passwordRulesFragment on PasswordRules { minLength charTypes }"""
ME = """query me($includeAccountConsentToken: Boolean!) { me { account { __typename ...accountGraphFragment } activeSession { __typename ...sessionGraphFragment } identity { __typename ...identityGraphFragment } } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo backgroundAudio prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }  fragment accountGraphFragment on Account { id umpMessages { data { messages { messageId messageSource displayLocations content } } } accountConsentToken @include(if: $includeAccountConsentToken) activeProfile { id umpMessages { data { messages { messageId content } } } } profiles { __typename ...profileGraphFragment } profileRequirements { primaryProfiles { personalInfo { requiresCollection } } secondaryProfiles { personalInfo { requiresCollection } personalInfoJrMode { requiresCollection } } } parentalControls { isProfileCreationProtected } flows { star { isOnboarded } } attributes { email emailVerified userVerified maxNumberOfProfilesAllowed locations { manual { country } purchase { country } registration { geoIp { country } } } } }  fragment sessionGraphFragment on Session { sessionId device { id } entitlements experiments { featureId variantId version } features { coPlay download noAds } homeLocation { countryCode adsSupported } inSupportedLocation isSubscriber location { countryCode adsSupported } portabilityLocation { countryCode } preferredMaturityRating { impliedMaturityRating ratingSystem } }  fragment identityGraphFragment on Identity { id email repromptSubscriberAgreement attributes { passwordResetRequired } commerce { notifications { subscriptionId type showNotification offerData { productType expectedTransition { date price { amount currency } } cypherKeys { key value type } } currentOffer { offerId price { amount currency frequency } } } } flows { marketingPreferences { isOnboarded eligibleForOnboarding } personalInfo { eligibleForCollection requiresCollection } } personalInfo { dateOfBirth gender } locations { purchase { country } } subscriber { subscriberStatus subscriptionAtRisk overlappingSubscription doubleBilled doubleBilledProviders subscriptions { id groupId state partner isEntitled source { sourceProvider sourceType subType sourceRef } product { id sku name entitlements { id name partner } bundle subscriptionPeriod earlyAccess trial { duration } categoryCodes } stacking { status overlappingSubscriptionProviders previouslyStacked previouslyStackedByProvider } term { purchaseDate startDate expiryDate nextRenewalDate pausedDate churnedDate isFreeTrial } } } consent { id idType token } }"""
REFRESH_TOKEN = """mutation refreshToken($refreshToken: RefreshTokenInput!) { refreshToken(refreshToken: $refreshToken) { activeSession { sessionId } } }"""
REGISTER_DEVICE = """mutation ($registerDevice: RegisterDeviceInput!) { registerDevice(registerDevice: $registerDevice) { __typename } }"""
REQUESET_OTP = """mutation requestOtp($input: RequestOtpInput!) { requestOtp(requestOtp: $input) { accepted } }"""
SET_IMAX = """mutation updateProfileImaxEnhancedVersion($input: UpdateProfileImaxEnhancedVersionInput!, $includeProfile: Boolean!) { updateProfileImaxEnhancedVersion(updateProfileImaxEnhancedVersion: $input) { accepted profile @include(if: $includeProfile) { __typename ...profileGraphFragment } } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo backgroundAudio prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }"""
SET_REMASTERED_AR = """mutation updateProfileRemasteredAspectRatio($input: UpdateProfileRemasteredAspectRatioInput!, $includeProfile: Boolean!) { updateProfileRemasteredAspectRatio(updateProfileRemasteredAspectRatio: $input) { accepted profile @include(if: $includeProfile) { __typename ...profileGraphFragment } } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo backgroundAudio prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }"""
SWITCH_PROFILE = """mutation switchProfile($input: SwitchProfileInput!, $includeIdentity: Boolean!, $includeAccountConsentToken: Boolean!) { switchProfile(switchProfile: $input) { account { __typename ...accountGraphFragment } activeSession { __typename ...sessionGraphFragment } identity @include(if: $includeIdentity) { __typename ...identityGraphFragment } } }  fragment profileGraphFragment on Profile { id name personalInfo { dateOfBirth gender } maturityRating { ratingSystem ratingSystemValues contentMaturityRating maxRatingSystemValue isMaxContentMaturityRating suggestedMaturityRatings { minimumAge maximumAge ratingSystemValue } } isAge21Verified flows { star { eligibleForOnboarding isOnboarded } personalInfo { eligibleForCollection requiresCollection } } attributes { isDefault kidsModeEnabled languagePreferences { appLanguage playbackLanguage preferAudioDescription preferSDH subtitleLanguage subtitlesEnabled } parentalControls { isPinProtected kidProofExitEnabled liveAndUnratedContent { enabled available } } playbackSettings { autoplay backgroundVideo backgroundAudio prefer133 preferImaxEnhancedVersion } avatar { id userSelected } privacySettings { consents { consentType value } } } }  fragment accountGraphFragment on Account { id umpMessages { data { messages { messageId messageSource displayLocations content } } } accountConsentToken @include(if: $includeAccountConsentToken) activeProfile { id umpMessages { data { messages { messageId content } } } } profiles { __typename ...profileGraphFragment } profileRequirements { primaryProfiles { personalInfo { requiresCollection } } secondaryProfiles { personalInfo { requiresCollection } personalInfoJrMode { requiresCollection } } } parentalControls { isProfileCreationProtected } flows { star { isOnboarded } } attributes { email emailVerified userVerified maxNumberOfProfilesAllowed locations { manual { country } purchase { country } registration { geoIp { country } } } } }  fragment sessionGraphFragment on Session { sessionId device { id } entitlements experiments { featureId variantId version } features { coPlay download noAds } homeLocation { countryCode adsSupported } inSupportedLocation isSubscriber location { countryCode adsSupported } portabilityLocation { countryCode } preferredMaturityRating { impliedMaturityRating ratingSystem } }  fragment identityGraphFragment on Identity { id email repromptSubscriberAgreement attributes { passwordResetRequired } commerce { notifications { subscriptionId type showNotification offerData { productType expectedTransition { date price { amount currency } } cypherKeys { key value type } } currentOffer { offerId price { amount currency frequency } } } } flows { marketingPreferences { isOnboarded eligibleForOnboarding } personalInfo { eligibleForCollection requiresCollection } } personalInfo { dateOfBirth gender } locations { purchase { country } } subscriber { subscriberStatus subscriptionAtRisk overlappingSubscription doubleBilled doubleBilledProviders subscriptions { id groupId state partner isEntitled source { sourceProvider sourceType subType sourceRef } product { id sku name entitlements { id name partner } bundle subscriptionPeriod earlyAccess trial { duration } categoryCodes } stacking { status overlappingSubscriptionProviders previouslyStacked previouslyStackedByProvider } term { purchaseDate startDate expiryDate nextRenewalDate pausedDate churnedDate isFreeTrial } } } consent { id idType token } }"""
UPDATE_DEVICE = """mutation updateDeviceOperatingSystem($updateDeviceOperatingSystem: UpdateDeviceOperatingSystemInput!) {updateDeviceOperatingSystem(updateDeviceOperatingSystem: $updateDeviceOperatingSystem) {accepted}}"""

class DisneyPlus(BaseService):
    """
    Service code for Disney+ Streaming Service (https://disneyplus.com).\n
    Version: 26.02.13

    Author: Made by CodeName393 with Special Thanks to narakama\n
    Authorization: Credentials\n
    Security: UHD@L1/SL3000 FHD@L1/SL3000 HD@L3/SL2000
    """

    ALIASES = ["DSNP", "disneyplus", "disney+"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?disneyplus\.com(?:/(?!browse)[a-z0-9-]+)?(?:/(?!browse)[a-z0-9-]+)?/(browse)/(?P<id>entity-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))(?:\?.*)?$",
        r"^(?:https?://(?:www\.)?disneyplus\.com(?:/[a-z0-9-]+)?(?:/[a-z0-9-]+)?/(movies|series)/[a-z0-9-]+/)?(?P<id>[a-zA-Z0-9-]+)(?:\?.*)?$",
    ]

    @staticmethod
    @click.command(name="DisneyPlus", short_help="https://disneyplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("--imax", is_flag=True, default=False, help="Prefer IMAX Enhanced version if available.")
    @click.option("--remastered-ar", is_flag=True, default=False, help="Prefer Remastered Aspect Ratio if available.")
    @click.option("-ext", "--extras", is_flag=True, default=False, help="Select a extras video if available.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return DisneyPlus(ctx, **kwargs)

    def __init__(self, ctx, title, imax, remastered_ar, extras):
        super().__init__(ctx)
        m = self.parse_title(ctx, title)
        self.title_id = m["id"]
        self.prefer_imax = imax
        self.prefer_remastered_ar = remastered_ar
        self.extras = extras

        self.vcodec = ctx.parent.params["vcodec"] or "H264"
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.wanted = ctx.parent.params["wanted"]
        self.quality = ctx.parent.params["quality"] or 1080

        self.playready = hasattr(ctx.obj.cdm, "certificate_chain") # ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY
        self.is_l3 = (ctx.obj.cdm.security_level < 3000) if self.playready else (ctx.obj.cdm.security_level == 3)

        self.region = None
        self.prod_config = {}
        self.account_tokens = {}
        self.token_expires_at = 0
        self.active_session = {}
        self.playback_data = {}

        assert ctx.parent is not None

        self.configure()

    def configure(self):
        self.log.info("Preparing...")

        if self.is_l3:
            self.vcodec = "H264"
            self.range = "SDR"
            self.quality = 720
            self.log.warning(" + Switched video to HD. This CDM only support HD.")
        else:
            if self.quality > 1080 and self.range == "SDR":
                self.range = "HDR10"
                self.log.info(" + Switched range to HDR10. 4K resolution requires HDR.")

            if (self.range != "SDR" or self.quality > 1080) and self.vcodec != "H265":
                self.vcodec = "H265"
                self.log.info(f" + Switched video codec to H265 to be able to get {self.range} dynamic range.")

            if self.acodec == "DTS" and not self.prefer_imax:
                self.prefer_imax = True
                self.log.info(" + Switched IMAX prefer. DTS audio can only be get from IMAX prefer.")

        self.session.headers.update({
            "User-Agent": self.config["bamsdk"]["user_agent"],
            "Accept-Encoding": "gzip",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

        ip_info = get_ip_info(self.session, fresh=True)
        country_key = None
        possible_keys = ["countryCode", "country", "country_code", "country-code"]
        for key in possible_keys:
            if key in ip_info:
                country_key = key
                break
        if country_key:
            self.region = str(ip_info[country_key]).upper()
            if self.region == "UNITED STATES":
                self.region = "US"
            self.log.info(f" + IP Region: {self.region}")
        else:
            self.log.warning(f" - The region could not be determined from IP information: {ip_info}")
            self.region = "US"
            self.log.info(f" + IP Region: {self.region} (By Default)")

        self.prod_config = self.session.get(self.config["endpoints"]["config"]).json()

        self.session.headers.update({
            "X-Application-Version": self.config["bamsdk"]["application_version"],
            "X-BAMSDK-Client-ID": self.config["bamsdk"]["client"],
            "X-BAMSDK-Platform": self.config["device"]["platform"],
            "X-BAMSDK-Version": self.config["bamsdk"]["sdk_version"],
            "X-DSS-Edge-Accept": "vnd.dss.edge+json; version=2",
            "X-Request-Yp-Id": self.config["bamsdk"]["yp_service_id"]
        })

        self.log.info("Logging into Disney+...")
        self._login()

        if self.config.get("profile") and "index" in self.config["profile"]:
            try:
                target_profile_index = int(self.config["profile"]["index"])
            except (ValueError, TypeError, KeyError):
                raise self.log.exit(" - Profile index in configuration is invalid.")

            profiles = self.active_session["account"]["profiles"]
            if not 0 <= target_profile_index < len(profiles):
                raise self.log.exit(f" - Invalid profile index: {target_profile_index}. Please choose between 0 and {len(profiles) - 1}.")
            
            target_profile = profiles[target_profile_index]
            active_profile_id = self.active_session["account"]["activeProfile"]["id"]

            if target_profile["id"] != active_profile_id:
                self._perform_switch_profile(target_profile, self.session.headers)

                self.log.info(" + Refreshing session data after profile switch...")
                full_account_info = self._get_account_info()
                self.active_session = full_account_info["activeSession"]
                self.active_session["account"] = full_account_info["account"]
                self.log.info("Session data updated successfully.")

        self.log.debug(self.active_session)

        if not self.active_session["isSubscriber"]:
            raise self.log.exit(" - Cannot continue, account is not subscribed to Disney+")
        if not self.active_session["inSupportedLocation"]:
            raise self.log.exit(" - Cannot continue, Not available in your Region.")

        self.log.info(f" + Account ID: {self.active_session['account']['id']}")
        self.log.info(f" + Profile ID: {self.active_session['account']['activeProfile']['id']}")
        self.log.info(f" + Subscribed: {self.active_session['isSubscriber']}")
        self.log.debug(f" + Account Region: {self.active_session['homeLocation']['countryCode']}")
        self.log.debug(f" + Detected Location: {self.active_session['location']['countryCode']}")
        self.log.debug(f" + Supported Location: {self.active_session['inSupportedLocation']}")

        active_profile_id = self.active_session["account"]["activeProfile"]["id"]
        full_profile_object = next(
            p for p in self.active_session["account"]["profiles"] if p["id"] == active_profile_id
        )

        current_imax_setting = full_profile_object["attributes"]["playbackSettings"]["preferImaxEnhancedVersion"]
        self.log.info(f" + IMAX Enhanced: {current_imax_setting}")
        if current_imax_setting is not self.prefer_imax:
            update_tokens = self._set_imax_preference(self.prefer_imax)
            self._apply_new_tokens(update_tokens["token"])

        current_133_setting = full_profile_object["attributes"]["playbackSettings"]["prefer133"] # Original Aspect Ratio
        self.log.info(f" + Remastered Aspect Ratio: {not current_133_setting}")
        if not current_133_setting is not self.prefer_remastered_ar:
            update_tokens = self._set_remastered_ar_preference(self.prefer_remastered_ar)
            self._apply_new_tokens(update_tokens["token"])

    def _login(self):
        tokens_cache_path = self.get_cache(f"tokens_{self.region}_{self.credentials.sha1}.json")
        cookie_dir = os.path.join(directories.cookies, "DisneyPlus")
        token_txt = os.path.join(cookie_dir, "token.txt")
        token_json = os.path.join(cookie_dir, "token.json")

        loaded_manual_token = False
        if os.path.isfile(token_json):
            try:
                self.log.info(" + Loading tokens from cookies/DisneyPlus/token.json...")
                with open(token_json, encoding="utf-8") as fd:
                    data = json.load(fd)
                if "token" in data:
                    data = data["token"]
                elif "extensions" in data and "sdk" in data["extensions"] and "token" in data["extensions"]["sdk"]:
                    data = data["extensions"]["sdk"]["token"]
                bearer = data.get("accessToken") or data.get("access_token")
                if bearer:
                    self.account_tokens = data
                    self.token_expires_at = time.time() + (data.get("expiresIn") or 86400)
                    self.session.headers.update({"Authorization": f"Bearer {bearer}"})
                    os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
                    with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                        json.dump(self.account_tokens, fd)
                    loaded_manual_token = True
            except Exception as e:
                self.log.warning(f" - Failed to load token.json ({e})")

        elif os.path.isfile(token_txt):
            try:
                self.log.info(" + Loading token from cookies/DisneyPlus/token.txt...")
                with open(token_txt, encoding="utf-8") as fd:
                    lines = [l.strip() for l in fd.read().splitlines() if l.strip()]
                if lines:
                    bearer = lines[0].replace("Bearer ", "").strip()
                    refresh = lines[1].strip() if len(lines) > 1 else None
                    self.account_tokens = {
                        "accessToken": bearer,
                        "refreshToken": refresh,
                        "expiresIn": 86400
                    }
                    self.token_expires_at = time.time() + 86400
                    self.session.headers.update({"Authorization": f"Bearer {bearer}"})
                    os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
                    with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                        json.dump(self.account_tokens, fd)
                    loaded_manual_token = True
            except Exception as e:
                self.log.warning(f" - Failed to load token.txt ({e})")

        if not loaded_manual_token:
            if os.path.isfile(tokens_cache_path):
                try:
                    self.log.info(" + Using cached tokens...")
                    with open(tokens_cache_path, encoding="utf-8") as fd:
                        self.account_tokens = json.load(fd)

                    cache_mod_time = os.path.getmtime(tokens_cache_path)
                    expires_in = self.account_tokens.get("expiresIn") or self.account_tokens.get("expires_in") or 0
                    self.token_expires_at = cache_mod_time + expires_in - 60

                    bearer = self.account_tokens.get("accessToken") or self.account_tokens.get("access_token")
                    if not bearer:
                        raise ValueError("accessToken not found in cache")
                    self.session.headers.update({"Authorization": f"Bearer {bearer}"})

                except (KeyError, ValueError, TypeError) as e:
                    self.log.warning(f" - Cached token data is invalid or corrupted ({e}). Getting new tokens...")
                    self._perform_full_login()

                try:
                    self._refresh()
                except Exception as e:
                    self.log.warning(f" - Failed to refresh token from cache ({e}). Getting new tokens...")
                    self._perform_full_login()

                # No problem if don't use it
                # self._update_device()

            else:
                self.log.info(" + Getting new tokens...")
                self._perform_full_login()

        self.log.info(" + Fetching session data...")
        full_account_info = self._get_account_info()
        self.active_session = full_account_info["activeSession"]
        self.active_session["account"] = full_account_info["account"]
        self.log.info("Session data setup successfully.")

    def _perform_full_login(self):
        android_id = secrets.token_bytes(8).hex()
        drm_id = f"{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('utf-8')}\n"
        device_token = self._register_device(android_id, drm_id)

        check_result = self._check_email(self.credentials.username, device_token)
        operations = [op.lower() for op in check_result.get("operations", [])]
        self.log.debug(f" + Check operations: {operations}")

        login_tokens = None
        if self.credentials.password and "register" not in operations:
            try:
                self.log.info(" + Logging in with password...")
                login_tokens = self._login_with_password(
                    self.credentials.username, self.credentials.password, device_token, raise_error=True
                )
            except Exception as e:
                self.log.warning(f" - Password login failed: {e}")

        if not login_tokens:
            if "otp" in operations:
                self.log.warning(" - Account requires OTP code login.")
                self._request_otp(self.credentials.username, device_token)

                otp_code = None
                try:
                    otp_code = input("Enter a OTP code (Check email): ")
                    if not otp_code:
                        self.log.exit("  - OTP code is required, but no value was entered.")
                    if not otp_code.isdigit():
                        self.log.exit("  - Invalid OTP code. Please enter only numbers.")
                    if len(otp_code) < 6:
                        self.log.exit("  - OTP code is too short. Please enter at least 6 digits.")
                    if len(otp_code) > 6:
                        self.log.warning("  - OTP code is longer than 6 digits. Using the first 6 digits.")
                        otp_code = otp_code[:6]
                except KeyboardInterrupt:
                    self.log.exit("\n - OTP code input cancelled by user.")
                
                auth_action = self._auth_action_with_otp(self.credentials.username, otp_code, device_token)
                login_tokens = self._login_with_auth_action(auth_action, device_token)

            elif "register" in operations:
                raise self.log.exit(" - Account is not registered. Please register first.")
            else:
                raise self.log.exit(f" - Email operations are {operations}. Account status verification required.")

        temp_auth_header = {"Authorization": f"Bearer {login_tokens['token']['accessToken']}"}
        account_info = self._get_account_info(temp_auth_header)
        profiles = account_info["account"]["profiles"]

        selected_profile = None
        if self.config.get("profile") and "index" in self.config["profile"]:
            try:
                profile_index = int(self.config["profile"]["index"])
                if not 0 <= profile_index < len(profiles):
                    raise ValueError(f"Index out of range (0-{len(profiles)-1})")
                
                selected_profile = profiles[profile_index]
            except (ValueError, TypeError):
                raise self.log.exit(" - Profile index in configuration is invalid.")
        else:
            selected_profile = next(
                (p for p in profiles if not p["attributes"]["kidsModeEnabled"] and not p["attributes"]["parentalControls"]["isPinProtected"]),
                None
            )
            if not selected_profile:
                raise self.log.exit(" - Auto-selection failed: No suitable profile found (non-kids, no PIN). Please configure a specific profile.")

        if selected_profile:
            self._perform_switch_profile(selected_profile, temp_auth_header)

    def _perform_switch_profile(self, target_profile, auth_headers):
        self.log.info(f" + Switching to profile: {target_profile['name']}({target_profile['id']})")

        if target_profile["attributes"]["kidsModeEnabled"]:
            raise self.log.exit("   - Kids Profile and cannot be used.")

        profile_pin = None
        if target_profile["attributes"]["parentalControls"]["isPinProtected"]:
            self.log.warning("   - This profile is PIN protected.")
            try:
                profile_pin = input("Enter a profile pin: ")
                if not profile_pin:
                    raise self.log.exit("   - PIN is required, but no value was entered.")
                if not profile_pin.isdigit():
                    raise self.log.exit("   - Invalid PIN. Please enter only numbers.")
                if len(profile_pin) < 4:
                    raise self.log.exit("   - PIN is too short. Please enter at least 4 digits.")
                if len(profile_pin) > 4:
                    self.log.warning("   - PIN is longer than 4 digits. Using the first 4 digits.")
                    profile_pin = profile_pin[:4]
            except KeyboardInterrupt:
                raise self.log.exit("\n - PIN input cancelled by user.")

        switch_profile_data = self._switch_profile(target_profile["id"], auth_headers, profile_pin)
        self._apply_new_tokens(switch_profile_data["token"])
        
    def _refresh(self):
        if hasattr(self, "token_expires_at") and time.time() < self.token_expires_at:
            self.log.debug(f" + Token is valid until: {datetime.fromtimestamp(self.token_expires_at).strftime('%Y-%m-%d %H:%M:%S')}")
            return self.session.headers.get("Authorization", "Bearer ").split(" ")[1]

        self.log.warning(" + Token expired. Refreshing...")
        refresh_token = self.account_tokens.get("refreshToken") or self.account_tokens.get("refresh_token")
        if not refresh_token:
            self.log.warning(" - No refreshToken available; attempting to use current accessToken.")
            return self.session.headers.get("Authorization", "Bearer ").split(" ")[1]
        try:
            refreshed_data = self._refresh_token(refresh_token)
            self._apply_new_tokens(refreshed_data["token"])
        except Exception as _:
            raise Exception("Refresh Token Expired")
        
    def _apply_new_tokens(self, token_data):
        self.account_tokens = token_data
        
        expires_in = self.account_tokens["expiresIn"] or 3600
        self.token_expires_at = time.time() + expires_in - 60
        self.log.debug(f"  + New Token is valid until: {datetime.fromtimestamp(self.token_expires_at).strftime('%Y-%m-%d %H:%M:%S')}")

        bearer = self.account_tokens["accessToken"]
        if not bearer:
            raise ValueError("Invalid token data: accessToken not found.")
        self.session.headers.update({"Authorization": f"Bearer {bearer}"})
        
        tokens_cache_path = self.get_cache(f"tokens_{self.region}_{self.credentials.sha1}.json")
        os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
        with open(tokens_cache_path, "w", encoding="utf-8") as fd:
            json.dump(self.account_tokens, fd)
            
        return bearer

    def get_titles(self):
        if not self.extras:
            try:
                content_info = self._get_deeplink(self.title_id)
                content_type = content_info["data"]["deeplink"]["actions"][0]["contentType"]
            except Exception as e:
                try:
                    actions_info = self._get_deeplink_last(self.title_id)
                    if actions_info["data"]["deeplink"]["actions"][0]["type"] == "browse":
                        info_block = base64.b64decode(actions_info["data"]["deeplink"]["actions"][0]["infoBlock"])
                        if b"movie" in info_block:
                            content_type = "movie"
                        elif b"series" in info_block:
                            content_type = "series"
                        else:
                            content_type = "other"
                            self.log.warning(" - The content is not standard. however, it tries to look up the data.")
                except Exception as e:
                    raise self.log.exit(f" - Failed to determine content type via deeplink ({e}).")
        else:
            content_type = "extras"
        self.log.debug(f" + Content Type: {content_type.upper()}")

        page = self._get_page(self.title_id)

        year = None
        if year_data := page["visuals"]["metastringParts"].get("releaseYearRange"):
            year = year_data.get("startYear")

        if content_type != "extras":
            playback_action = next(
                (x for x in page["actions"] if x["type"] == "playback"),
                None
            )
            if not playback_action:
                self.log.exit(f" - No content is available. (Playback action not found)", exc_info=False)
            lang_data = self._get_original_lang(playback_action["availId"])
            player_exp = lang_data["data"]["playerExperience"]
            orig_lang = player_exp.get("originalLanguage") or player_exp.get("targetLanguage") or "en"
            self.log.debug(f" + Original Language: {orig_lang}")
        
        if content_type in ("movie", "other"):
            return Title(
                id_=page["id"],
                type_=Title.Types.MOVIE,
                name=page["visuals"]["title"],
                year=year,
                original_lang=orig_lang,
                source=self.ALIASES[0],
                service_data=page
            )

        elif content_type == "series":
            return self._get_series(page, year, orig_lang)

        elif content_type == "extras":
            return self._get_extras(page, year)
        else:
            raise self.log.exit(f" - Unsupported content type: {content_type}")

    def _get_series(self, page, year, orig_lang):
        container = next(x for x in page["containers"] if x["type"] == "episodes")
        season_ids = [s["id"] for s in container["seasons"]]

        titles = []
        for season_id in season_ids:
            episodes_data =  self._get_episodes_data(season_id)

            for ep in episodes_data:
                if ep["type"] != "view":
                    continue

                titles.append(
                    Title(
                        id_=ep["id"],
                        type_=Title.Types.TV,
                        name=ep["visuals"]["title"],
                        season=int(ep["visuals"]["seasonNumber"]),
                        episode=int(ep["visuals"]["episodeNumber"]),
                        episode_name=ep["visuals"]["episodeTitle"],
                        year=year,
                        original_lang=orig_lang,
                        source=self.ALIASES[0],
                        service_data=ep
                    )
                )
            
        return titles
    
    def _get_extras(self, page, year):
        extras_containers = [
            x for x in page["containers"]
            if x["type"] == "set" and x["style"]["name"] == "standard_compact_list"
        ]

        if not extras_containers:
            raise self.log.exit(" - No extras found.")

        extras_episodes = []
        ep_count = 1

        first_item = extras_containers[0]["items"][0]
        first_action = next((x for x in first_item["actions"] if x["type"] in ("playback", "trailer")), None)
        if first_action:
            lang_data = self._get_original_lang(first_action["availId"])
            player_exp = lang_data["data"]["playerExperience"]
            orig_lang = player_exp.get("originalLanguage") or player_exp.get("targetLanguage") or "en"
            self.log.debug(f" + Original Language: {orig_lang}")

        for container in extras_containers:
            items = container["items"]
            for item in items:
                if item["type"] == "view":
                    action = next((x for x in item["actions"] if x["type"] in ("playback", "trailer")), None)
                    
                    if action:
                        extras_episodes.append(
                            Title(
                                id_=item["id"],
                                type_=Title.Types.TV,
                                name=item["visuals"]["title"],
                                season=0, # Special
                                episode=ep_count,
                                episode_name=item["visuals"]["title"],
                                year=year,
                                original_lang=orig_lang,
                                source=self.ALIASES[0],
                                service_data=item
                            )
                        )
                        ep_count += 1

        if not extras_episodes:
            raise self.log.exit(" - No playable extras found.")

        return extras_episodes

    def get_tracks(self, title):
        playback = next(x for x in title.service_data["actions"] if x["type"] == "playback")
        media_id = playback["resourceId"] or None
        if not media_id:
            raise self.log.exit(f" - Failed to get media ID for playback info")

        total_duration = title.service_data["visuals"]["metastringParts"]["runtime"]["runtimeMs"]
        scenario = "ctr-regular" if self.is_l3 else "ctr-high" # cbcs-high

        self.log.debug(f" + Playback Scenario: {scenario}")
        self.log.debug(f" + Media ID: {media_id}")

        self._refresh() # Safe Access

        if (self.range == "DV+HDR" or self.range == "HYBRID") and not self.is_l3:
            self.log.warning(" + DV+HDR Multi-range requested.")

            self.log.info("   + Fetching Dolby Vision tracks...")
            tracks = self._fetch_manifest_tracks(title, media_id, scenario, ["DOLBY_VISION"])

            self.log.info("   + Fetching HDR10 tracks...")
            hdr_tracks_temp = self._fetch_manifest_tracks(title, media_id, scenario, ["HDR10"]) # HDR10PLUS

            tracks.add(hdr_tracks_temp, warn_only=True)
        else:
            video_ranges = []
            if not self.is_l3:
                if self.range == "DV":
                    video_ranges = ["DOLBY_VISION"]
                elif self.range == "HDR10":
                    video_ranges = ["HDR10"] # HDR10PLUS

            tracks = self._fetch_manifest_tracks(title, media_id, scenario, video_ranges or None)
        
        return self._post_process_tracks(tracks, total_duration)

    def _fetch_manifest_tracks(self, title, media_id, scenario, video_ranges=None):
        attributes = {
            "codecs": {
                "supportsMultiCodecMaster": False,
                "video": ["h.264"]
            },
            "protocol": "HTTPS",
            "frameRates": [60],
            "assetInsertionStrategies": {
                "point": "SGAI", # Server-Guided Ad Insertion
                "range": "SGAI" # Server-Guided Ad Insertion
            },
            "playbackInitiationContext": "ONLINE",
            "slugDuration": "SLUG_500_MS", # SLUG_1000_MS, SLUG_750_MS ?
            "maxSlideDuration": "4_HOUR" # 15_MIN ?
        }

        if self.is_l3:
            attributes["resolution"] = {"max": ["1280x720"]}
        else:
            attributes["resolution"] = {"max": ["3840x2160"]}

            if self.vcodec == "H265":
                attributes["codecs"]["supportsMultiCodecMaster"] = True
                attributes["codecs"]["video"] = ["h.265"]

            attributes["audioTypes"] = ["ATMOS", "DTS_X"]
            
            if video_ranges:
                attributes["videoRanges"] = video_ranges

        payload = {
            "playbackId": media_id,
            "playback": {
                "attributes": attributes
            }
        }
        self.playback_data[title.id] = self._get_playback(scenario, payload)
        manifest_url = self.playback_data[title.id]["sources"][0]["complete"]["url"]
        self.log.debug(f" + Manifest URL: {manifest_url}")
        manifest_res = requests.get(manifest_url, headers={"User-Agent": self.session.headers.get("User-Agent")})
        if manifest_res.status_code != 200:
            raise self.log.exit(f" - Failed to fetch manifest: {manifest_res.status_code} {manifest_res.text[:200]}")
        manifest = manifest_res.text
        return Tracks.from_m3u8(
            m3u8.loads(content=manifest, uri=manifest_url),
            source=self.ALIASES[0]
        )

    def _post_process_tracks(self, tracks, total_duration):
        for video in tracks.videos:
            video.size = int((total_duration * video.bitrate) / 8000)

        final_audios = []
        for audio in tracks.audios:
            bitrate_match = re.search(r"(?<=composite_)\d+|\d+(?=_(?:hdri|complete))|(?<=-)\d+(?=K/)", as_list(audio.url)[0])
            if bitrate_match:
                audio.bitrate = int(bitrate_match.group()) * 1000
                if audio.bitrate == 1_000_000:
                    audio.bitrate = 768_000 # DSNP lies about the Atmos bitrate
            
            audio.size = int((total_duration * audio.bitrate) / 8000)
            # No longer supported
            if not (audio.codec == "EC3" and audio.channels == 2.0):
                final_audios.append(audio)
        
        tracks.audios = final_audios

        return tracks

    def get_chapters(self, title):
        try:
            editorial = self.playback_data[title.id]["editorial"]
            if not editorial:
                return []
            
            LABEL_MAP = {
                "intro_start": "intro_start",
                "intro_end": "intro_end",
                "recap_start": "recap_start",
                "recap_end": "recap_end",
                "FFER": "recap_start", # First Frame Episode Recap
                "LFER": "recap_end", # Last Frame Episode Recap 
                "FFEI": "intro_start", # First Frame Episode Intro
                "LFEI": "intro_end", # Last Frame Episode Intro
                "FFEC": "credits_start", # First Frame End Credits
                "LFEC": "lfec_marker", # Last Frame End Credits
                "FFCB": None, # First Frame Credits Bumper
                "LFCB": None, # Last Frame Credits Bumper
                "up_next": None,
                "tag_start": None,
                "tag_end": None,
            }
            
            NAME_MAP = {
                "recap_start": "Recap",
                "recap_end": "Scene",
                "intro_start": "Intro",
                "intro_end": "Scene",
                "credits_start": "Credits",
            }

            grouped = {}
            for marker in editorial:
                group = LABEL_MAP.get(marker["label"])
                offset = marker["offsetMillis"]
                if group and offset is not None:
                    grouped.setdefault(group, []).append(offset)

            raw_chapters = []
            total_runtime = title.service_data["visuals"]["metastringParts"]["runtime"]["runtimeMs"]

            for group, times in grouped.items():
                if not times: continue

                timestamp = min(times) if "start" in group else max(times) if "end" in group else times[0]
                name = NAME_MAP.get(group)
                
                if group == "lfec_marker" and (total_runtime - timestamp) > 5000:
                    name = "Scene"

                if name:
                    raw_chapters.append((timestamp, name))

            raw_chapters.sort(key=lambda x: x[0])
            unique_chapters = []
            seen_ms = set()
            
            for ms, name in raw_chapters:
                if ms not in seen_ms:
                    unique_chapters.append({"ms": ms, "name": name})
                    seen_ms.add(ms)

            if not unique_chapters:
                unique_chapters.append({"ms": 0, "name": "Scene"})
            else:
                first = unique_chapters[0]
                if first["ms"] > 0:
                    if first["ms"] < 5000 and first["name"] in ("Intro", "Recap"):
                        first["ms"] = 0
                    else:
                        unique_chapters.insert(0, {"ms": 0, "name": "Scene"})

            # Create Final Chapter List
            final_chapters = []
            scene_count = 0
            for i, chap_info in enumerate(unique_chapters):
                name = chap_info["name"]
                if name == "Scene":
                    scene_count += 1
                    name = f"Scene {scene_count}"
                
                timecode = (datetime(1, 1, 1) + timedelta(milliseconds=chap_info["ms"])).strftime("%H:%M:%S.%f")[:-3]
                final_chapters.append(
                    MenuTrack(
                        number=i + 1,
                        title=name,
                        timecode=timecode
                    )
                )
            
            return final_chapters

        except Exception as e:
            self.log.warning(f"Failed to extract chapters: {e}")
            return []

    def certificate(self, **_):
        # endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["widevineCertificate"]["href"]
        # res = self.session.get(endpoint, data=challenge)
        return None if self.playready else self.config["certificate"]

    def license(self, challenge, **_):
        self._refresh() # Safe Access

        if self.playready:
            self.log.debug(" + Requesting PlayReady License")
            endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["playReadyLicense"]["href"]
            headers = {
                "Accept": "application/xml, application/vnd.media-service+json; version=2",
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"
            }
        else:
            self.log.debug(" + Requesting Widevine License")
            endpoint = self.prod_config["services"]["drm"]["client"]["endpoints"]["widevineLicense"]["href"]
            headers = {"Content-Type": "application/octet-stream"}

        res = self.session.post(endpoint, headers=headers, data=challenge)
        if res.status_code != 200:
            if error := res.json().get("errors"):
                raise self.log.exit(f" - License request failed: {error[0]}")
        return res.content
    
    def _get_deeplink(self, ref_id):
        endpoint = self._href(
            self.prod_config["services"]["content"]["client"]["endpoints"]["getDeeplink"]["href"],
            refIdType="deeplinkId",
            refId=ref_id
        )
        data =  self._request("GET", endpoint)
        return data
    
    def _get_deeplink_last(self, ref_id):
        endpoint = self._href(self.prod_config["services"]["explore"]["client"]["endpoints"]["getDeeplink"]["href"])
        params = {
            "refIdType" : "deeplinkId",
            "refId" : ref_id
        }
        data =  self._request("GET", endpoint, params=params)
        return data

    def _get_page(self, title_id):
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getPage"]["href"],
            pageId=title_id
        )
        data = self._request("GET", endpoint, params={"disableSmartFocus": "true", "limit": 999})
        return data["data"]["page"]

    def _get_original_lang(self, availId):
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getPlayerExperience"]["href"],
            availId=availId
        )
        data =  self._request("GET", endpoint)
        return data
    
    def _get_episodes_data(self, season_id):
        endpoint = self._href(
            self.prod_config["services"]["explore"]["client"]["endpoints"]["getSeason"]["href"],
            seasonId=season_id
        )
        data = self._request("GET", endpoint, params={"limit": 999})["data"]["season"]["items"]
        return data

    def _get_playback(self, scenario, payload):
        endpoint = self._href(
            self.prod_config["services"]["media"]["client"]["endpoints"]["mediaPayload"]["href"],
            scenario=scenario
        )
        headers = {
            "Accept": "application/vnd.media-service+json",
            "X-DSS-Feature-Filtering": "true"
        }
        data = self._request("POST", endpoint, headers=headers, payload=payload)
        return data["stream"]

    def _register_device(self, android_id, drm_id):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["registerDevice"]["href"]
        headers = {
            "Authorization": self.config["bamsdk"]["api_key"],
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "variables": {
                "registerDevice": {
                    "applicationRuntime": self.config["device"]["applicationRuntime"], 
                    "attributes": {
                        "osDeviceIds": [
                            {
                                "identifier": android_id,
                                "type": "android.vendor.id"
                            },
                            {
                                "identifier": drm_id,
                                "type": "android.drm.id"
                            }
                        ],
                        "operatingSystem": self.config["device"]["operatingSystem"],
                        "operatingSystemVersion": self.config["device"]["operatingSystemVersion"]
                    },
                    "deviceFamily": self.config["device"]["family"], 
                    "deviceLanguage": self.config["device"]["deviceLanguage"], 
                    "deviceProfile": self.config["device"]["profile"],
                    "devicePlatformId": self.config["device"]["platform_id"],
                }
            },
            "query": REGISTER_DEVICE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]["token"]["accessToken"]

    def _check_email(self, email, token):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "check",
            "variables": {
                "email": email
            },
            "query": CHECK_EMAIL
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data.get("data", {}).get("check", {})

    def _login_with_password(self, email, password, token, raise_error=False):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "login",
            "variables": {
                "input": {
                    "email": email,
                    "password": password
                },
                "includeIdentity": True,
                "includeAccountConsentToken": True
            },
            "query": LOGIN
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers, raise_error=raise_error)
        return data["extensions"]["sdk"]

    def _request_otp(self, email, token):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "requestOtp",
            "variables": {
                "input": {
                    "email": email,
                    "reason": "Login"
                }
            },
            "query": REQUESET_OTP
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        if not data["data"]["requestOtp"]["accepted"]:
            raise self.log.exit(" - OTP code request failed.")

    def _auth_action_with_otp(self, email, otp, token):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "authenticateWithOtp",
            "variables": {
                "input": {
                    "email": email,
                    "passcode": otp
                }
            },
            "query": LOGIN_OTP
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["data"]["authenticateWithOtp"]["actionGrant"]
    
    def _login_with_auth_action(self, auth_action, token):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {
            "Authorization": token,
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "loginWithActionGrant",
            "variables": {
                "input": {
                    "actionGrant": auth_action
                },
                "includeAccountConsentToken": True
            },
            "query": LOGIN_ACTION_GRANT
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]

    def _get_account_info(self, headers={}):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers.update({"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]})
        payload = {
            "operationName": "me",
            "variables": {
                "includeAccountConsentToken": True
            },
            "query": ME
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["data"]["me"]

    def _switch_profile(self, profile_id, headers, pin=None):
        profile_input = {"profileId": profile_id}
        if pin: profile_input["entryPin"] = pin

        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers.update({"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]})
        payload = {
            "operationName": "switchProfile",
            "variables": {
                "input": profile_input,
                "includeIdentity": True,
                "includeAccountConsentToken": True
            },
            "query": SWITCH_PROFILE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]

    def _refresh_token(self, refresh_token):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["refreshToken"]["href"]
        headers = {
            "Authorization": self.config["bamsdk"]["api_key"],
            "X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]
        }
        payload = {
            "operationName": "refreshToken",
            "variables": {
                "refreshToken": {
                    "refreshToken": refresh_token
                }
            },
            "query": REFRESH_TOKEN
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        return data["extensions"]["sdk"]

    def _update_device(self, android_id, drm_id):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateDeviceOperatingSystem",
            "variables": {
                "updateDeviceOperatingSystem": {
                    "operatingSystem": self.config["device"]["operatingSystem"],
                    "operatingSystemVersion": self.config["device"]["operatingSystemVersion"],
                    "osDeviceIds": [
                        {
                            "identifier": android_id,
                            "type": "android.vendor.id"
                        },
                        {
                            "identifier": drm_id,
                            "type": "android.drm.id"
                        }
                    ]
                }
            },
            "query": UPDATE_DEVICE
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)

        if data["data"]["updateDeviceOperatingSystem"]["accepted"]:
            return data["extensions"]["sdk"]
        else:
            self.log.warning("   - Failed to update Device Operating System.")

    def _set_imax_preference(self, enabled):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateProfileImaxEnhancedVersion",
            "variables": {
                "input": {
                    "imaxEnhancedVersion": enabled,
                },
                "includeProfile": True
            },
            "query": SET_IMAX,
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        
        if data["data"]["updateProfileImaxEnhancedVersion"]["accepted"]:
            self.log.info(f"   + Updated IMAX Enhanced preference: {enabled}")
            return data["extensions"]["sdk"]
        else:
            self.log.warning("   - Failed to set IMAX preference.")

    def _set_remastered_ar_preference(self, enabled):
        endpoint = self.prod_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        headers = {"X-BAMSDK-Platform-Id": self.config["device"]["platform_id"]}
        payload = {
            "operationName": "updateProfileRemasteredAspectRatio",
            "variables": {
                "input": {
                    "remasteredAspectRatio": enabled,
                },
                "includeProfile": True
            },
            "query": SET_REMASTERED_AR,
        }
        data = self._request("POST", endpoint, payload=payload, headers=headers)
        
        if data["data"]["updateProfileRemasteredAspectRatio"]["accepted"]:
            self.log.info(f"   + Updated Remastered Aspect Ratio preference: {enabled}")
            return data["extensions"]["sdk"]
        else:
            self.log.warning("   - Failed to set Remastered Aspect Ratio preference.")

    def _href(self, href, **kwargs):
        _args = {"version": self.config["bamsdk"]["explore_version"]}
        _args.update(**kwargs)
        return href.format(**_args)
    
    def _request(self, method, endpoint, params=None, headers=None, payload=None, raise_error=False):
        _headers = self.session.headers.copy()
        if headers: _headers.update(headers)
        _headers.update({
            "X-BAMSDK-Transaction-ID": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4())
        })

        try:
            res = self.session.request(
                method=method,
                url=endpoint,
                headers=_headers,
                params=params,
                json=payload
            )
            res.raise_for_status()
            data = res.json()
            if data.get("errors"):
                error_code = data["errors"][0]["extensions"]["code"]
                if "token.service.invalid.grant" in error_code:
                    raise ConnectionError(f"Refresh Token Expired: {error_code}")
                elif "token.service.unauthorized.client" in error_code:
                    raise ConnectionError(f"Unauthorized Client/IP: {error_code}")
                elif "idp.error.identity.bad-credentials" in error_code:
                    raise ConnectionError(f"Bad Credentials: {error_code}")
                elif "account.profile.pin.invalid" in error_code:
                    raise ConnectionError(f"Invalid PIN: {error_code}")
                raise ConnectionError(data["errors"])
            if data.get("data") and data["data"].get("errors"):
                raise ConnectionError(data["data"]["errors"])
            return data
        except Exception as e:
            if raise_error or "Refresh Token Expired" in str(e) or "/deeplink" in endpoint:
                raise e
            else:
                raise self.log.exit(f" - API Request failed: {e}")