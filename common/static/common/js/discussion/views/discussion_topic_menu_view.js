/* globals Backbone, _ */

(function() {
    'use strict';
    if (Backbone) {
        this.DiscussionTopicMenuView = Backbone.View.extend({
            events: {
                'change .post-topic': 'handleTopicEvent'
            },

            attributes: {
                class: 'post-field'
            },

            initialize: function(options) {
                this.course_settings = options.course_settings;
                this.currentTopicId = options.topicId;
                _.bindAll(this,
                    'handleTopicEvent'
                );
                return this;
            },

            render: function() {
                var context = _.clone(this.course_settings.attributes);
                context.topics_html = this.renderCategoryMap(this.course_settings.get('category_map'));
                this.$el.html(_.template($('#topic-template').html())(context));
                if (this.getCurrentTopicId()) {
                    this.setTopic(this.$('option', '.post-topic').filter(
                        '[data-discussion-id="' + this.getCurrentTopicId() + '"]')
                    );
                } else {
                    this.setTopic(this.$('option', '.post-topic').first());
                }
                return this.$el;
            },

            renderCategoryMap: function(map) {
                var categoryTemplate = _.template($('#new-post-menu-category-template').html()),
                    entryTemplate = _.template($('#new-post-menu-entry-template').html());

                return _.map(map.children, function(name) {
                    var entry,
                        html = '';
                    if (_.has(map.entries, name)) {
                        entry = map.entries[name];
                        html = entryTemplate({
                            text: name,
                            id: entry.id,
                            is_cohorted: entry.is_cohorted
                        });
                    } else { // subcategory
                        html = categoryTemplate({
                            text: name,
                            entries: this.renderCategoryMap(map.subcategories[name])
                        });
                    }
                    return html;
                }, this).join('');
            },

            handleTopicEvent: function(event) {
                this.setTopic($(event.target));
                return this;
            },

            setTopic: function($target) {
                var $option = $('option:selected', $target);

                if ($option.data('discussion-id')) {
                    this.topicText = this.getFullTopicName($option);
                    this.currentTopicId = $option.data('discussion-id');
                    this.trigger('thread:topic_change', $option);
                }
                return this;
            },

            getCurrentTopicId: function() {
                return this.currentTopicId;
            },

            /**
             * Return full name for the `topicElement` if it is passed.
             * Otherwise, full name for the current topic will be returned.
             * @param {jQuery Element} [topicElement]
             * @return {String}
             */
            getFullTopicName: function(topicElement) {
                var name;
                if (topicElement) {
                    name = topicElement.html();
                    _.each(topicElement.parents('optgroup'), function(item) {
                        name = $(item).attr('label') + ' / ' + name;
                    });
                    return name;
                } else {
                    return this.topicText;
                }
            }
        });
    }
}).call(this);
